"""What the released 0.2.2 leaves of an import of older data, and this version's import after it.

Review rule 4, run rather than read (AB-1's class, at startup). The import
(`legacy_migration.migrate`) converts the retired Noctalia plugin's data into
this application's files: each target is written to a hidden stage beside it
(``.<target>.legacy-migration-v1.stage``), a journal records the set, and each
stage is then hard-linked to its public name and unlinked. The released import
is the producer here, in a child (`tests.released`), under one fault at a
time (`tests.released_faults`); then a person may edit any one file it left.
This checkout is the consumer: it looks (`probe`, which a window's "ask me
again" also runs), imports, and imports again.

What must hold, and is asserted: the older data itself is byte for byte as it
was; an import the released code left half done, with nothing edited,
completes here; nothing that was ever at a public name, and nothing a person
edited at one, loses a byte; looking writes nothing; and every path a
message names exists.

Known, and not asserted either way: a stage a person edited is not kept. A
resumed import rewrites a stage that is not exactly its converted output
(`_stage_targets`), and removes a stage whose target is already installed
without reading it (`_install_targets`). That is left as it is, by decision:
a stage was never public; it sits under a hidden name in the application's own
folders; it holds only this application's conversion of the older data, which
the import can make again and never touches; and the code is the released
0.2.2's, unchanged, where changing the import this late would risk the
upgrade itself. The rewrite goes through a new file renamed over the stage's
name, so it cannot write through the hard link a stage briefly shares with
its public target; and if that shared file was edited, the resume refuses
first and changes nothing (`test_a_stage_that_is_also_the_public_file...`).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_legacy_migration import _legacy_fixture
from wall_in_one import config, legacy_migration
from wall_in_one.library import playlists

#: The released producer: the import, from the older data where it is.
PRODUCER = r"""
from wall_in_one import file_io, legacy_migration

trace_filesystem((file_io, "_rename_noreplace"))


def operation():
    try:
        print("DONE", legacy_migration.migrate().detail, flush=True)
    except legacy_migration.MigrationError as error:
        print("REFUSED", error, flush=True)


serve(operation)
"""

STAGE_SUFFIX = ".legacy-migration-v1.stage"


class _Consumer:
    def __init__(self) -> None:
        #: What the person had before any import, per profile.
        self.older: dict[Path, faults.Tree] = {}

    def prepare(self, base: Path) -> faults.Tree:
        """A profile with the retired plugin's data, its media, and Noctalia's settings."""
        with pytest.MonkeyPatch.context() as scoped:
            faults.scratch_profile(scoped, base)
            _legacy_fixture(base)
        self.older[base] = faults.tree(base)
        return self.older[base]

    @staticmethod
    def exempt(entry: faults.Entry, _before: faults.Tree) -> bool:
        # A stage's name: the known case (see the module's words), and the
        # second name of a public file, whose bytes its public name must keep.
        return entry.path.name.endswith(STAGE_SUFFIX)

    def _older_data(self, base: Path) -> dict[Path, bytes]:
        """The older data as it is now (a person may have edited it since)."""
        now = faults.tree(base)
        return {
            entry.path: found.data
            for entry in self.older[base].entries
            if (found := now.at(entry.path)) is not None
        }

    def _older_data_changed(self, base: Path, was: dict[Path, bytes]) -> str:
        now = self._older_data(base)
        changed = sorted(str(path.relative_to(base)) for path in was if now.get(path) != was[path])
        return f"FINDING the older data changed: {changed}" if changed else ""

    def consume(self, base: Path) -> Iterator[tuple[str, str]]:
        was = self._older_data(base)
        before = faults.shape(faults.tree(base), base)
        try:
            looked = legacy_migration.probe()
            said = f"{looked.status}: {looked.detail}"
        except legacy_migration.MigrationError as error:
            said = f"REFUSED {error}"
        if faults.shape(faults.tree(base), base) != before:
            said = f"FINDING looking wrote something: {said}"
        yield "look", said

        for name in ("import", "import again"):
            try:
                said = legacy_migration.migrate().detail
            except legacy_migration.MigrationError as error:
                said = f"REFUSED {error}"
            yield name, self._older_data_changed(base, was) or said


def _campaign(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    consumer = _Consumer()

    def plans(trace: faults.Produced) -> list[dict[str, object]]:
        # A kill before every change. In full: each error at every call, each
        # edit of what was just published with what follows it, and someone
        # else changing each of the first three files before every change.
        if not full:
            return [{"kill": call.index} for call in trace.changes]
        planned = faults.single_faults(trace, full=True)
        planned += faults.edits_while_public(trace, full=True)
        for call in trace.changes:
            planned += [
                {"meddle": call.index, "which": which, "how": how}
                for which in range(3)
                for how in (*faults.EDITS, "replace")
            ]
        return planned

    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        (),
        tmp_path,
        prepare=consumer.prepare,
        consume=consumer.consume,
        exempt=consumer.exempt,
        plans=plans,
        edited_after=lambda plan: not plan or "kill" in plan,
        every=3 if full else 8,
        full=full,
    )
    print("\n" + found.summary("import of older data"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_nothing_a_released_import_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs, under the bounded plans."""
    _campaign(released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_nothing_a_released_import_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _campaign(released_source, tmp_path, monkeypatch, full=True)


def _killed_at_each_change(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    consumer: _Consumer,
) -> list[tuple[faults.Job, faults.Produced]]:
    plain = faults.Job(tmp_path / "plain", {}, ())
    consumer.prepare(plain.base)
    (trace,) = faults.run(released_source, PRODUCER, [plain])
    assert trace.status == 0 and trace.output.startswith("DONE imported"), trace.output
    jobs = [
        faults.Job(tmp_path / f"k{number:04d}", {"kill": call.index}, ())
        for number, call in enumerate(trace.changes)
    ]
    for job in jobs:
        consumer.prepare(job.base)
    return list(zip(jobs, faults.run(released_source, PRODUCER, jobs), strict=True))


def test_an_import_the_released_code_left_half_done_completes_here(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Killed before each of its changes in turn, with nothing edited: this
    version's import finishes every one, on the older data as it was.

    An import the released code had journaled is finished with the bytes it
    staged: its journal binds each target's digest, the report's included,
    and the released report ends with a "Rollback" paragraph this version no
    longer writes. One it had not journaled yet is this version's own, and
    its report has no such paragraph."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    consumer = _Consumer()
    states = _killed_at_each_change(released_source, tmp_path, consumer)
    assert any(
        entry.path.name.endswith(STAGE_SUFFIX) and entry.path.stat().st_nlink == 2
        for job, _made in states
        for entry in faults.tree(job.base).entries
    ), "one state had a stage that was also its public target"

    began: set[str] = set()
    for job, made in states:
        assert made.killed, made.output + made.errors[-2000:]
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            was = consumer._older_data(job.base)
            released_began = (
                legacy_migration.journal_path().exists() or legacy_migration.marker_path().exists()
            )
            outcome = legacy_migration.migrate()
            where = f"killed before call {job.plan['kill']}: {outcome.detail}"
            # Imported now; or it already was, and only the journal was left.
            assert outcome.detail.startswith(
                ("imported legacy authoring", "legacy authoring was already imported")
            ), where
            assert legacy_migration.marker_path().is_file(), where
            assert not legacy_migration.journal_path().exists(), where
            # No stage is left, but one: killed between the marker's link and
            # its stage's unlink, the import is known done by the marker before
            # any stage is looked at, and the marker keeps that second name.
            left = [
                entry.path
                for entry in faults.tree(job.base).entries
                if entry.path.name.endswith(STAGE_SUFFIX)
            ]
            marker = legacy_migration.marker_path()
            assert all(os.path.samefile(stage, marker) for stage in left), where
            assert not consumer._older_data_changed(job.base, was), where
            # The report is the one the import that began it staged, to the byte.
            report = legacy_migration.report_path().read_bytes()
            recorded = json.loads(marker.read_bytes())["targets"]["report"]["sha256"]
            assert hashlib.sha256(report).hexdigest() == recorded, where
            assert (b"## Rollback" in report) == released_began, where
            began.add("the released version" if released_began else "this version")
            # What it installed is what this version reads.
            assert config.load_strict(require_present=True).roots, where
            assert playlists.Store.open().fault is None, where
            assert not legacy_migration.migrate().changed, "and a second import has nothing to do"
    assert began == {"the released version", "this version"}, "both kinds of state were met"


def test_a_stage_that_is_also_the_public_file_is_never_written_through(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between its link and its unlink a stage and its public target are one
    file. Edited in place then (through either name), the resumed import
    refuses and changes nothing; unedited, it completes, and the public file
    is the same file with the same bytes."""
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    consumer = _Consumer()
    shared = 0
    for edit in (False, True):
        for job, _made in _killed_at_each_change(
            released_source, tmp_path / ("edited" if edit else "as left"), consumer
        ):
            linked = [
                entry
                for entry in faults.tree(job.base).entries
                if entry.path.name.endswith(STAGE_SUFFIX) and entry.path.stat().st_nlink == 2
            ]
            if not linked:
                continue
            shared += 1
            (stage,) = linked
            public = stage.path.with_name(
                stage.path.name.removeprefix(".").removesuffix(STAGE_SUFFIX)
            )
            assert public.stat().st_ino == stage.inode
            if edit:
                faults.edit_in_place(stage.path)
            expected = public.read_bytes()
            with monkeypatch.context() as scoped:
                faults.scratch_profile(scoped, job.base)
                was = consumer._older_data(job.base)
                if edit:
                    # "... exists with different or unsafe bytes; it was not
                    # overwritten"; or, the marker itself, no longer a marker.
                    with pytest.raises(legacy_migration.MigrationError):
                        legacy_migration.migrate()
                    assert stage.path.read_bytes() == expected
                else:
                    assert legacy_migration.migrate().detail.startswith(
                        ("imported legacy authoring", "legacy authoring was already imported")
                    )
                    assert public == legacy_migration.marker_path() or not stage.path.exists()
            assert public.stat().st_ino == stage.inode, "the same file"
            assert public.read_bytes() == expected
            with monkeypatch.context() as scoped:
                faults.scratch_profile(scoped, job.base)
                assert not consumer._older_data_changed(job.base, was)
    assert shared >= 2
