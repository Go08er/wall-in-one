"""No room on the disk, at the steps `tests/test_disk_full.py` does not reach.

Three steps that only run after something else has already happened, so a
fault at them is a second event and the campaigns there never land on it:

- Tidy up's **Undo**: a leftover a real download left (0.2.x's own stage, or
  this version's commit name) is archived first, with room; then Undo runs
  with ENOSPC (in full, EDQUOT too) at each of its writes (before a byte and
  after half of them), syncs, moves and folders made.
- The download record's **put-back**. A removal withdraws the record into a
  claim before it touches the media and puts it back if it doesn't commit:
  here the media can't be claimed (a stub in the producer, not a fault of
  the campaign), so the put-back is the step the full disk meets. And replay
  does the same for a removal that died after withdrawing the record: that
  start is run under the full disk.
- `file_io._retain_exact_entry` called directly on a file at a public name:
  its folder made, its move, each sync.

What must hold: nothing that was ever public is short or lost; a message
names where things really are; and with room again the next ordinary
operation works with no repair by hand (Undo finishes; the record is at its
name again; the file can be retained).
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_removals as removals
from tests import test_tidy_undo_across_versions as undoing
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_disk_full import _sound
from tests.test_released_round_trip import REMOVAL, SOURCE, STORES
from wall_in_one import file_io

RECORD = "aurora.mp4.motionbgs.json"

# -- `_retain_exact_entry`, directly ---------------------------------------------------------

#: A file at a public name moved into the retained folder. ``first``: the
#: folder is made by this call; ``again``: it is there already.
RETAIN = r"""
import os
import stat
from pathlib import Path

from wall_in_one import file_io

trace_filesystem((file_io, "_rename_noreplace"))


def operation(layout):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    disarm()
    path = root / "aurora.mp4.motionbgs.json"
    path.write_bytes(b'{"a record": "that was public, and a person may have edited"}')
    if layout == "again":
        (root / file_io.RETAINED_ENTRY_DIRECTORY).mkdir(mode=0o700)
    current = file_io.regular_file_fingerprint(path)
    had(path)
    arm()
    try:
        kept = file_io._retain_exact_entry(
            path,
            expected_identity=current[:2],
            expected_file_type=stat.S_IFREG,
            expected_fingerprint=current,
        )
        print("RETAINED kept at", kept, flush=True)
    except file_io.PathChangedError as error:
        where = "" if error.preserved_path is None else f" [preserved at {error.preserved_path}]"
        print("REFUSED PathChangedError", str(error) + where, flush=True)
    except OSError as error:
        print("REFUSED OSError", error, flush=True)


serve(operation)
"""


def _every_landing(trace: faults.Produced, *, full: bool) -> list[dict[str, object]]:
    """`faults.disk_full`, but at every change and sync even for a gate: a short step."""
    plans = faults.disk_full(trace, full=True)
    return plans if full else [plan for plan in plans if plan["errno"] == "ENOSPC"]


def _retain_again(base: Path) -> Iterator[tuple[str, str]]:
    path = Path(faults.profile_environment(base)["HOME"]) / "Wallpapers" / RECORD
    retained = path.parent / file_io.RETAINED_ENTRY_DIRECTORY
    held = sorted(retained.iterdir()) if retained.is_dir() else []
    if path.is_file() and any(entry.is_file() and entry.stat().st_size for entry in held):
        yield "as the full disk left it", "FINDING it is at its name and in the retained folder"
    else:
        yield "as the full disk left it", "at its name" if path.is_file() else "retained"
    if not path.is_file():
        yield "retain it, with room again", "already retained"
        return
    current = file_io.regular_file_fingerprint(path)
    try:
        kept = file_io._retain_exact_entry(
            path,
            expected_identity=current[:2],
            expected_file_type=stat.S_IFREG,
            expected_fingerprint=current,
        )
    except OSError as error:
        yield "retain it, with room again", f"FINDING it can't be retained now either: {error}"
    else:
        yield "retain it, with room again", f"retained, kept at {kept}"


@pytest.mark.parametrize("layout", ["first", "again"])
def test_a_full_disk_while_an_entry_is_retained_loses_and_misnames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    full = bool(os.environ.get(faults.FULL))
    found = faults.campaign(
        monkeypatch,
        SOURCE,
        RETAIN,
        (layout,),
        tmp_path,
        prepare=downloads._prepare,
        consume=_retain_again,
        # A short step: in full, also a kill before each change and EIO or
        # EACCES at every call (a look that fails right after the move).
        plans=lambda trace: (
            _every_landing(trace, full=full)
            + (faults.single_faults(trace, full=True) if full else [])
        ),
        edited_after=lambda _plan: False,
        producer_says=True,
        full=full,
    )
    print("\n" + found.summary(f"no room: an entry retained ({layout})"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


# -- the download record's put-back ----------------------------------------------------------

_SERVE = "serve(operation)"
assert REMOVAL.rstrip().endswith(_SERVE)
#: The removal as this version runs it, with the media refusing its claim: the
#: record is withdrawn, the removal can't commit, and the record is put back.
#: ``PUTBACK n`` says which call is the first after the claim was refused.
REMOVAL_REFUSED = (
    REMOVAL.rstrip().removesuffix(_SERVE)
    + r"""
import errno

_claim_for_deletion = file_io.claim_for_deletion


def _media_refuses(path, **keywords):
    logical = keywords.get("logical_path")
    if logical is not None and Path(logical).name == "aurora.mp4":
        print("PUTBACK", _STATE["calls"] + 1, flush=True)
        raise OSError(errno.EIO, "the media could not be claimed", str(logical))
    return _claim_for_deletion(path, **keywords)


file_io.claim_for_deletion = _media_refuses

"""
    + _SERVE
    + "\n"
)

#: A start of this version: the journal replayed, then the library scanned.
REPLAY = r"""
import os
from pathlib import Path

from wall_in_one import config, file_io
from wall_in_one.session import Session

trace_filesystem((file_io, "_rename_noreplace"))


def operation():
    try:
        session = Session(config.load_strict(require_present=True))
        try:
            failures = session.retry_removals()
            session.refresh()
        finally:
            session.shutdown()
        print("REPLAYED", "; ".join(failures) or "nothing to say", flush=True)
    except OSError as error:
        print("REFUSED OSError", error, flush=True)
    except Exception as error:
        print("RAISED", type(error).__name__, error, flush=True)


serve(operation)
"""


def _record_claims(base: Path) -> list[Path]:
    """Every claim folder under the download folder that holds something."""
    folder = removals._root(base) / "Wall-in-One" / "MotionBGS"
    return sorted(
        entry
        for claim in folder.glob(f"{file_io.DELETION_CLAIM_PREFIX}*")
        if claim.is_dir()
        for entry in claim.iterdir()
        if entry.is_file() and entry.stat().st_size
    )


class _RecordBack:
    """This version's starts with room again, and where the download record is then."""

    def __init__(self) -> None:
        self.consumer = removals._Consumer("remove")

    def prepare(self, base: Path) -> faults.Tree:
        return removals._prepare(base)

    def exempt(self, entry: faults.Entry, before: faults.Tree) -> bool:
        # By name too: outside a scratch profile the stores' own paths are elsewhere.
        return entry.path.name in STORES or self.consumer.exempt(entry, before)

    def consume(self, base: Path) -> Iterator[tuple[str, str]]:
        yield "as the full disk left it", _sound(base)
        media = self.consumer.media(base)
        record = media.with_name(RECORD)
        there = media.is_file()
        whole = [
            entry.data
            for entry in faults.tree(base).entries
            if entry.path.name in (RECORD, "entry") and entry.data.startswith(b"{")
        ]
        yield from self.consumer.consume(base)
        if not media.is_file():
            yield "the record afterwards", "the removal finished: gone with the media"
        elif not there:
            yield "the record afterwards", "FINDING the media came back from nowhere"
        elif record.is_file() and record.read_bytes() in whole:
            claims = _record_claims(base)
            yield (
                "the record afterwards",
                "FINDING back at its name, and a copy still in a claim"
                if any(claim.read_bytes() in whole for claim in claims)
                else "back at its name, byte for byte",
            )
        elif any(claim.read_bytes() in whole for claim in _record_claims(base)):
            # Whole, and named by the removal's own message; but nothing puts it back.
            yield "the record afterwards", f"{STRANDED} after two starts with room"
        else:
            yield "the record afterwards", "FINDING neither at its name nor whole in a claim"


#: The download record left for good in its claim (see the strict xfail below).
STRANDED: Final = "STRANDED whole in its claim"


def _putback_plans(trace: faults.Produced, *, full: bool) -> list[dict[str, object]]:
    """A full disk at the put-back and whatever follows it; in full, anywhere."""
    marked = re.search(r"^PUTBACK (\d+)$", trace.output, re.MULTILINE)
    assert marked is not None, "the media's claim was refused: " + trace.output
    first = int(marked.group(1))
    plans = _every_landing(trace, full=full)
    if full:
        return plans

    def at(plan: dict[str, object]) -> int:
        number = plan.get("fail", plan.get("short"))
        assert isinstance(number, int)
        return number

    return [plan for plan in plans if at(plan) >= first]


def _a_removal_puts_its_record_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> faults.Campaign:
    undoing._quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    back = _RecordBack()
    found = faults.campaign(
        monkeypatch,
        SOURCE,
        REMOVAL_REFUSED,
        ("remove",),
        tmp_path,
        prepare=back.prepare,
        consume=back.consume,
        exempt=back.exempt,
        plans=lambda trace: _putback_plans(trace, full=full),
        edited_after=lambda _plan: False,
        producer_says=True,
        full=full,
    )
    print("\n" + found.summary("no room: a removal's record put-back"))
    return found


@pytest.fixture(scope="module")
def put_back(tmp_path_factory: pytest.TempPathFactory) -> faults.Campaign:
    """The campaign, once for the tests that read it."""
    with pytest.MonkeyPatch.context() as patch:
        return _a_removal_puts_its_record_back(tmp_path_factory.mktemp("put-back"), patch)


def test_a_full_disk_while_a_removal_puts_its_record_back(put_back: faults.Campaign) -> None:
    """Nothing is short or lost, the record is whole wherever it is, and the
    removal's own words name where that is."""
    assert any("back at its name" in said for said in put_back.outcomes), "a put-back was reached"
    assert not put_back.findings, "\n".join(sorted(set(put_back.findings))[:30])


@pytest.mark.xfail(
    strict=True, reason="removal follow-up on r5-ac12b: put-back failure keeps the intent"
)
def test_a_record_whose_put_back_failed_is_put_back_by_a_later_start(
    put_back: faults.Campaign,
) -> None:
    """The media could not be claimed, and then the record's move back to its
    name failed for want of room. Today the removal still cancels its pending
    intent, so no later start looks for the record: it stays in
    ``.wall-in-one-removal-<token>/entry`` for good, and the wallpaper is a
    local file from then on. With the intent kept, the next start's replay
    puts it back."""
    assert not put_back.findings, "\n".join(sorted(set(put_back.findings))[:30])
    assert not [said for said in put_back.outcomes if STRANDED in said]


def _replay_puts_a_record_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Counter[str], list[str], list[str]]:
    """What each step said, what went wrong, and each message that named the
    claim as where a record is kept when the record was at its name by then."""
    undoing._quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    back = _RecordBack()
    plain = faults.Job(tmp_path / "plain", {}, ("remove",))
    back.prepare(plain.base)
    (alone,) = faults.run(SOURCE, REMOVAL, [plain])
    assert alone.status == 0, alone.output + alone.errors[-2000:]
    # Which kills leave the record in a claim with the media still at its name.
    probes = [
        faults.Job(tmp_path / f"k{call.index:05d}", {"kill": call.index}, ("remove",))
        for call in alone.changes
    ]
    for job in probes:
        back.prepare(job.base)
    faults.run(SOURCE, REMOVAL, probes)
    died = [
        job
        for job in probes
        if back.consumer.media(job.base).is_file()
        and not back.consumer.media(job.base).with_name(RECORD).exists()
        and _record_claims(job.base)
    ]
    assert died, "one kill left the record withdrawn and the media at its name"
    outcomes: Counter[str] = Counter()
    findings: list[str] = []
    misnamed: list[str] = []
    tried = 0
    for state, job in enumerate(died if full else died[:: max(1, len(died) // 3)]):
        traced = faults.Job(tmp_path / f"s{state:02d}-trace", job.plan, ("remove",))
        back.prepare(traced.base)
        faults.run(SOURCE, REMOVAL, [traced])
        (trace,) = faults.run(SOURCE, REPLAY, [faults.Job(traced.base)])
        assert trace.status == 0 and trace.output.startswith("REPLAYED"), trace.output
        plans = _every_landing(trace, full=full)
        jobs = [
            faults.Job(tmp_path / f"s{state:02d}-a{number:05d}", job.plan, ("remove",))
            for number, _plan in enumerate(plans)
        ]
        firsts = [back.prepare(each.base) for each in jobs]
        dead = faults.run(SOURCE, REMOVAL, jobs)
        befores = [faults.tree(each.base) for each in jobs]
        starts = faults.run(
            SOURCE,
            REPLAY,
            [faults.Job(each.base, plan) for each, plan in zip(jobs, plans, strict=True)],
        )
        tried += len(plans)
        for each, plan, first, made, before, start in zip(
            jobs, plans, firsts, dead, befores, starts, strict=True
        ):
            label = f"died at {job.plan}, then a start under {plan}"
            said = start.output if start.status == 0 else f"RAISED it ended {start.status}"
            told = faults._RANDOM.sub("*", said.replace(str(each.base), ""))
            outcomes[f"the start under the full disk: {told[:170]}"] += 1
            if said.startswith("RAISED"):
                findings.append(f"FINDING {label}: {said} {start.errors[-600:]}")
            record = back.consumer.media(each.base).with_name(RECORD)
            for path in _said_kept(said, each.base):
                if os.path.lexists(path):
                    continue
                if "could not be made durable" in said and record.is_file():
                    # The move back landed and only its sync failed: the record
                    # was at its name when this was said (AC-5).
                    misnamed.append(f"{label}: {said.replace(str(each.base), '')}")
                elif not record.is_file():
                    # (At its name by now: the start's second replay, in its
                    # scan, met no fault and finished what this one reported.)
                    findings.append(
                        f"SAID {label}: names {path.relative_to(each.base)}, which isn't there"
                    )
            protected = (
                frozenset(entry.inode for entry in first.entries)
                | made.once_public
                | start.once_public
            )
            after = faults.tree(each.base)
            for entry in faults.lost(
                before,
                after,
                protected,
                lambda entry, before=before: back.exempt(entry, before),  # type: ignore[misc]
            ):
                findings.append(
                    f"LOST {label}: the {entry.size} bytes of "
                    f"{entry.path.relative_to(each.base)} are nowhere"
                )
            with monkeypatch.context() as scoped:
                faults.scratch_profile(scoped, each.base)
                earlier = after
                for name, words in back.consume(each.base):
                    now = faults.tree(each.base)
                    told = faults._RANDOM.sub("*", words.replace(str(each.base), ""))
                    outcomes[f"{name}: {told[:170]}"] += 1
                    if words.startswith("FINDING") or STRANDED in words:
                        findings.append(f"{words} [{label}, at {name}]")
                    for entry in faults.lost(
                        earlier,
                        now,
                        protected,
                        lambda entry, earlier=earlier: back.exempt(entry, earlier),  # type: ignore[misc]
                    ):
                        findings.append(
                            f"LOST {label}: after {name}, the {entry.size} bytes of "
                            f"{entry.path.relative_to(each.base)} are nowhere"
                        )
                    earlier = now
    print(f"\nno room: replay's record put-back: {len(died)} states died there, {tried} faults")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {count:5d}  {outcome}")
    print(f"  messages naming the claim for a record that was back at its name: {len(misnamed)}")
    return outcomes, findings, misnamed


def _said_kept(said: str, base: Path) -> list[Path]:
    """Each path ``said`` gives as where something was kept, preserved, left or moved."""
    return [
        path
        for claim in faults._KEPT_AT.finditer(said)
        for path in faults._named(said[claim.end() :], base)[:1]
    ]


@pytest.fixture(scope="module")
def replayed(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Counter[str], list[str], list[str]]:
    """The campaign, once for the tests that read it."""
    with pytest.MonkeyPatch.context() as patch:
        return _replay_puts_a_record_back(tmp_path_factory.mktemp("replayed"), patch)


def test_a_full_disk_while_replay_puts_a_record_back(
    replayed: tuple[Counter[str], list[str], list[str]],
) -> None:
    """A removal died after withdrawing its record and before the media moved;
    the next start, which puts the record back, meets the full disk; the two
    starts after it have room, and the record is at its name again."""
    outcomes, findings, _misnamed = replayed
    assert any("back at its name" in said for said in outcomes)
    assert not findings, "\n".join(sorted(set(findings))[:30])


def test_replay_names_where_a_record_put_back_without_its_sync_really_is(
    replayed: tuple[Counter[str], list[str], list[str]],
) -> None:
    """Replay's move of the record back to its name landed and the sync after it
    failed. The claim is empty and the record is at its name, and that is what
    is said: never "the download record kept at <claim>/entry could not be put
    back" (AC-5)."""
    _outcomes, findings, misnamed = replayed
    assert not findings, "\n".join(sorted(set(findings))[:30])
    assert not misnamed, "\n".join(misnamed[:5])


# -- Tidy up's Undo ----------------------------------------------------------------------------


def test_a_full_disk_during_undo_loses_nothing_and_undo_finishes_with_room(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    undoing._quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    probe = undoing._Probe()
    # One state of each kind (in full, every one): 0.2.x's own stage left by
    # its download, and this version's commit name left by its own.
    chosen = undoing.leftover_states(released_source, tmp_path)
    tried = 0
    #: A failed Undo that had already put items back before it failed.
    unsaid = 0
    for kind, states in sorted(chosen.items()):
        for state, (source, job) in enumerate(states if full else states[:1]):
            tag = f"{kind.split()[-1]}{state}"

            def made(
                names: list[str], source: Path = source, job: faults.Job = job
            ) -> list[faults.Job]:
                return undoing.again(source, job, tmp_path, names)

            (traced,) = made([f"{tag}-trace"])
            faults.run(SOURCE, undoing.TIDY, [faults.Job(traced.base, {}, ("archive",))])
            (trace,) = faults.run(SOURCE, undoing.TIDY, [faults.Job(traced.base, {}, ("undo",))])
            assert trace.output.startswith("UNDID"), trace.output + trace.errors[-1000:]
            plans = _every_landing(trace, full=full)
            jobs = made([f"{tag}-a{number:05d}" for number, _plan in enumerate(plans)])
            firsts = [faults.tree(each.base) for each in jobs]
            archives = faults.run(
                SOURCE, undoing.TIDY, [faults.Job(each.base, {}, ("archive",)) for each in jobs]
            )
            befores = [faults.tree(each.base) for each in jobs]
            undos = faults.run(
                SOURCE,
                undoing.TIDY,
                [
                    faults.Job(each.base, plan, ("undo",))
                    for each, plan in zip(jobs, plans, strict=True)
                ],
            )
            tried += len(plans)
            for each, plan, first, archive, before, undo in zip(
                jobs, plans, firsts, archives, befores, undos, strict=True
            ):
                label = f"{kind}, Undo under {plan}"
                assert archive.output.startswith("ARCHIVED"), archive.output
                probe.archived += undoing._kinds(before, each.base)
                after = faults.tree(each.base)
                said = undo.output if undo.status == 0 else f"RAISED it ended {undo.status}"
                back = sum(
                    first.at(entry.path) is not None and before.at(entry.path) is None
                    for entry in after.entries
                )
                unsaid += bool(back) and said.startswith("REFUSED")
                probe.said(
                    "Undo under the full disk",
                    said + (f" [{back} already back at its name]" if back else ""),
                    each.base,
                )
                probe.hop(label, "the Undo under the full disk", before, after, each.base)
                probe.undone(label, "this version", said, before, after, each.base)
                for path in _said_kept(said, each.base):
                    if not os.path.lexists(path):
                        probe.findings.append(
                            f"SAID {label}: names {path.relative_to(each.base)}, which isn't there"
                        )
                for manifest in after.named("manifest"):
                    try:
                        json.loads(manifest.data)
                    except ValueError:
                        if manifest.path.name == "manifest.json":
                            probe.findings.append(
                                f"FINDING {label}: {manifest.path.relative_to(each.base)} "
                                "was left short or unreadable"
                            )
                with monkeypatch.context() as scoped:
                    faults.scratch_profile(scoped, each.base)
                    again = undoing._tidy_here(each.base, "undo")
                    now = faults.tree(each.base)
                    probe.said("Undo again, with room", again, each.base)
                    probe.hop(label, "Undo again with room", after, now, each.base)
                    probe.undone(label, "this version", again, after, now, each.base)
                    # Whatever the first Undo managed, everything is back now.
                    astray = sorted(
                        str(entry.path.relative_to(each.base))
                        for entry in first.entries
                        if now.at(entry.path) != entry
                    )
                    if astray or again.startswith(("REFUSED", "RAISED")):
                        probe.findings.append(
                            f"FINDING {label}: with room again Undo left {astray} not back as "
                            f"it was: {again}"
                        )
                    once_more = undoing._tidy_here(each.base, "archive")
                    probe.said("archive again, with room", once_more, each.base)
                    if not once_more.startswith("ARCHIVED"):
                        probe.findings.append(f"FINDING {label}: archiving again: {once_more}")
    print("\n" + probe.summary(f"no room: Tidy up's Undo, {tried} faults"))
    print(f"  Undos that refused with items already back at their names: {unsaid}")
    assert probe.archived["a commit name"] and probe.archived["an older stage"]
    assert not probe.findings, "\n".join(sorted(set(probe.findings))[:30])
