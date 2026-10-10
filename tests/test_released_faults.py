"""The fault harness itself (`tests.released_faults`): it does what a probe relies on.

A campaign that finds nothing is only worth something if it would have found
something. So: the tracer really kills, fails and edits where its plan says;
and a consumer that empties what the released code left, or names a file that
isn't there, is reported.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_released_leftovers_stores import PRODUCER, _prepare
from wall_in_one.library import playlists

#: The released playlists store saving over a file a crashed editor left
#: damaged: it moves that file to ``.broken`` and publishes without replacing.
DAMAGED = ("playlists", "damaged", "second")


def _state(base: Path) -> Path:
    return Path(faults.profile_environment(base)["XDG_STATE_HOME"]) / "wall-in-one"


def test_the_tracer_kills_fails_and_edits_where_its_plan_says(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    plain = faults.Job(tmp_path / "plain", {}, DAMAGED)
    _prepare(plain.base)
    (trace,) = faults.run(released_source, PRODUCER, [plain])
    assert trace.status == 0 and trace.output == "DONE", trace.output + trace.errors
    kinds = {call.kind for call in trace.calls}
    assert kinds == {"change", "look", "sync"}, "every kind of call is numbered"
    moved = [call for call in trace.changes if call.name == "_rename_noreplace"]
    assert moved, "the released code's own rename is traced"
    broken = next(path for _call, path, _inode in trace.public if path.endswith(".broken"))
    published = next(call for call, path, _inode in trace.public if path.endswith(".json"))

    jobs = [
        faults.Job(tmp_path / "kill", {"kill": moved[0].index}, DAMAGED),
        faults.Job(tmp_path / "fail", {"fail": moved[0].index, "errno": "EACCES"}, DAMAGED),
        faults.Job(tmp_path / "edit", {"edit": published}, DAMAGED),
    ]
    for job in jobs:
        _prepare(job.base)
    killed, failed, edited = faults.run(released_source, PRODUCER, jobs)

    # Killed just before the damaged file was moved aside: it is still there.
    assert killed.killed and killed.output == ""
    assert len(killed.calls) == moved[0].index, "nothing ran after the kill"
    assert not (_state(jobs[0].base) / Path(broken).name).exists()
    # Failed there instead: the released code refused, through its own error path.
    assert failed.status == 0 and failed.output.startswith("REFUSED PlaylistError")
    assert "Permission denied" in failed.output
    # Edited right after it was published: the same file, one byte different.
    assert edited.status == 0
    (_call, path, inode) = edited.edited[0]
    target = _state(jobs[2].base) / "playlists.json"
    assert Path(path) == target and target.stat().st_ino == inode
    untouched = (_state(plain.base) / "playlists.json").read_bytes()
    changed = target.read_bytes()
    assert len(changed) == len(untouched)
    assert sum(a != b for a, b in zip(changed, untouched, strict=True)) == 1


def test_a_consumer_that_empties_or_misnames_a_leftover_is_reported(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)

    def careless(base: Path) -> Iterator[tuple[str, str]]:
        """What a sweep of old-looking names would do: empty them, and say it kept them."""
        emptied = []
        for path in sorted(_state(base).glob("playlists.json.broken*")):
            path.write_bytes(b"")
            emptied.append(path)
        yield "sweep", f"emptied {len(emptied)}"
        yield "say", f"the damaged file was kept at {_state(base) / 'playlists.json.kept'}"

    def kills(trace: faults.Produced) -> list[dict[str, object]]:
        return [{"kill": call.index} for call in trace.changes]

    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        DAMAGED,
        tmp_path / "careless",
        prepare=_prepare,
        consume=careless,
        plans=kills,
    )
    assert any(
        finding.startswith("LOST") and "playlists.json.broken" in finding
        for finding in found.findings
    ), found.findings[:5]
    assert any(finding.startswith("SAID") for finding in found.findings)

    def careful(base: Path) -> Iterator[tuple[str, str]]:
        yield "open", f"{playlists.Store.open().fault}"

    assert not faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        DAMAGED,
        tmp_path / "careful",
        prepare=_prepare,
        consume=careful,
        plans=kills,
    ).findings


def test_what_counts_as_lost(tmp_path: Path) -> None:
    kept, gone, private, empty = (tmp_path / name for name in ("kept", "gone", ".private", "empty"))
    kept.write_bytes(b"a person's file")
    gone.write_bytes(b"another, about to be emptied")
    private.write_bytes(b"a temporary nobody ever saw")
    empty.write_bytes(b"")
    # Two files a person has with the same bytes: settings, and a backup of them.
    original, backup = tmp_path / "settings", tmp_path / "settings.bak"
    original.write_bytes(b"the same bytes, twice")
    backup.write_bytes(b"the same bytes, twice")
    before = faults.tree(tmp_path)
    must = frozenset(entry.inode for entry in before.entries if entry.path != private)

    moved = tmp_path / ".retained"
    moved.mkdir()
    kept.rename(moved / "entry")  # moved whole: its bytes are still somewhere
    gone.write_bytes(b"")
    private.unlink()
    original.write_bytes(b"")  # emptied, though the backup still holds those bytes
    after = faults.tree(tmp_path)

    assert {entry.path for entry in faults.lost(before, after, must)} == {gone, backup}
    # (Which of the two equal files is named is by order; one of them is.)
    assert faults.lost(before, after, must, lambda entry: entry.path == backup) == [
        entry for entry in before.entries if entry.path == gone
    ]
