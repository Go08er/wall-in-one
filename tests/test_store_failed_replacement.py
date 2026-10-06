"""A damaged file moved aside, then not replaced, says where it went (Y-4).

The approved recovery for a store file whose content is damaged moves it
aside (`state_file.preserve_faulted`, to ``<name>.broken``) and then writes
what could be read plus the change. When that replacement can't be written,
the change didn't happen, but something did: the damaged original is no
longer at its name. Every message said "nothing changed". Now the store's
refusal says where the original is kept and that the new file wasn't saved,
and the window never adds "nothing changed" for a task that moved it.

Real faults on disposable files: bytes that aren't JSON, and the state
folder made read-only (mode 0500) right after the real move aside.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_published_not_durable import CASES, Case
from wall_in_one.library import state_file

DAMAGED = b"{ not json"
RECOVERING = [case for case in CASES if case.name != "pending removals"]


@contextlib.contextmanager
def read_only_after_the_move(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """The real move aside, then its folder can't take the replacement."""
    real = state_file.preserve_faulted
    folders: list[Path] = []

    def preserve(path: Path, **keywords: Any) -> Path:
        backup = real(path, **keywords)
        folders.append(path.parent)
        path.parent.chmod(0o500)
        return backup

    monkeypatch.setattr(state_file, "preserve_faulted", preserve)
    try:
        yield folders
    finally:
        for folder in folders:
            folder.chmod(0o700)


@pytest.mark.parametrize("case", RECOVERING, ids=[case.name for case in RECOVERING])
def test_a_damaged_file_moved_aside_then_not_replaced_says_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    target = tmp_path / "state" / case.filename
    target.parent.mkdir(mode=0o700)
    target.write_bytes(DAMAGED)
    store = case.open(target)
    assert store.fault is not None and not state_file.read_failed(store.fault)
    backup = target.with_name(target.name + ".broken")

    with read_only_after_the_move(monkeypatch) as folders, pytest.raises(Exception) as refused:
        case.mutate(store)

    assert folders == [target.parent], "the real move aside happened"
    message = str(refused.value)
    assert getattr(refused.value, "kind", None) == "local-io", message
    # Before: "local-io: could not write .../<name>: Permission denied", no word of the move.
    assert f"{case.filename} could not be used, so it was kept as {backup}" in message
    assert "its replacement was not saved" in message and "Permission denied" in message
    assert backup.read_bytes() == DAMAGED, "the original is safe at its backup"
    assert not target.exists()
