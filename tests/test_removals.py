"""The durable boundary between a removal gesture and eventual cleanup."""

from __future__ import annotations

import json
import multiprocessing
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from wall_in_one.library import removals, state_file
from wall_in_one.library.model import Kind, MediaItem


def _item(path: Path, kind: Kind = Kind.STILL) -> MediaItem:
    return MediaItem(path=path, kind=kind, size=1, mtime=1)


def _prepare_from_stale_process(
    journal: str,
    source: str,
    root: str,
    ready: Connection,
    proceed: Connection,
) -> None:
    store = removals.Store.open(Path(journal))
    ready.send(True)
    proceed.recv()
    store.prepare(_item(Path(source)), (Path(root),))


def _hold_lock_then_write(
    journal: str,
    source: str,
    root: str,
    ready: Connection,
    proceed: Connection,
) -> None:
    target = Path(journal)
    path = Path(source)
    status = path.lstat()
    source_root = Path(root)
    root_status = source_root.stat()
    parent_status = path.parent.stat()
    intent = removals.Intent(
        path=path,
        kind=Kind.STILL,
        roots=(source_root,),
        token=secrets.token_hex(16),
        device=status.st_dev,
        inode=status.st_ino,
        source_root=source_root,
        root_device=root_status.st_dev,
        root_inode=root_status.st_ino,
        parent_device=parent_status.st_dev,
        parent_inode=parent_status.st_ino,
    )
    with state_file.mutation_lock(target, description="pending-removals"):
        ready.send(True)
        proceed.recv()
        # Give an unlocked writer ample time to read/write the stale empty
        # document. A correctly locked Store remains blocked until this
        # process installs its own entry and releases the lock.
        time.sleep(0.1)
        removals._save({intent.identity: intent}, target)


def test_prepare_is_durable_and_commit_survives_reopen(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    source.parent.mkdir()
    source.write_bytes(b"image")
    target = tmp_path / "pending-removals.json"
    store = removals.Store.open(target)

    prepared = store.prepare(_item(source), (root,))
    committed = store.mark_committed(prepared)

    (reopened,) = removals.Store.open(target).records
    assert reopened == committed
    assert reopened.committed
    assert reopened.original_is_present()


def test_prepared_intent_remembers_the_exact_original_inode(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    source.parent.mkdir()
    source.write_bytes(b"first")
    intent = removals.Store.open(tmp_path / "pending.json").prepare(_item(source), (root,))

    assert intent.original_is_present()
    held = root / "old-inode"
    held.hardlink_to(source)
    source.unlink()
    source.write_bytes(b"replacement")

    assert not intent.original_is_present()


def test_faulted_journal_is_never_overwritten_by_prepare(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    source.parent.mkdir()
    source.write_bytes(b"image")
    target = tmp_path / "pending.json"
    target.write_text("not json", encoding="utf-8")

    store = removals.Store.open(target)

    assert store.fault is not None
    with pytest.raises(removals.RemovalJournalError, match="cannot safely update"):
        store.prepare(_item(source), (root,))
    assert target.read_text(encoding="utf-8") == "not json"


def test_prepare_refuses_too_many_roots_before_writing(tmp_path: Path) -> None:
    roots = tuple(tmp_path / f"root-{index}" for index in range(removals.MAX_ROOTS_PER_REMOVAL + 1))
    source = roots[0] / "paper.png"
    source.parent.mkdir()
    source.write_bytes(b"image")
    target = tmp_path / "pending.json"

    with pytest.raises(removals.RemovalJournalError, match="at most 32 library roots"):
        removals.Store.open(target).prepare(_item(source), roots)

    assert not target.exists()


def test_prepare_refuses_a_document_larger_than_the_reader_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    root.mkdir()
    source.write_bytes(b"image")
    target = tmp_path / "pending.json"
    monkeypatch.setattr(removals, "MAX_STATE_BYTES", 64)

    with pytest.raises(removals.RemovalJournalError, match="limited to 64 bytes"):
        removals.Store.open(target).prepare(_item(source), (root,))

    assert not target.exists()


def test_stale_discard_cannot_remove_a_newer_intent_for_the_same_path(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    root.mkdir()
    source.write_bytes(b"first")
    target = tmp_path / "pending.json"
    store = removals.Store.open(target)
    old = store.prepare(_item(source), (root,))
    store.discard(old)
    # Even the exact same inode is a new removal transaction. The random
    # token, not merely dev/inode, prevents a stale caller cancelling it.
    new = store.prepare(_item(source), (root,))

    with pytest.raises(removals.RemovalJournalError, match="different pending removal"):
        store.discard(old)

    assert removals.Store.open(target).records == (new,)
    store.discard(new)


def test_a_second_remover_cannot_share_an_existing_prepared_intent(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    source = root / "paper.png"
    root.mkdir()
    source.write_bytes(b"image")
    target = tmp_path / "pending.json"
    first = removals.Store.open(target)
    second = removals.Store.open(target)
    intent = first.prepare(_item(source), (root,))

    with pytest.raises(removals.RemovalJournalError, match="still in progress"):
        second.prepare(_item(source), (root,))

    assert removals.Store.open(target).records == (intent,)
    first.discard(intent)


def test_stale_process_rebases_without_losing_another_removal(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    first = root / "first.png"
    second = root / "second.png"
    root.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    target = tmp_path / "pending.json"
    parent = removals.Store.open(target)
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe()
    proceed_parent, proceed_child = context.Pipe()
    process = context.Process(
        target=_prepare_from_stale_process,
        args=(str(target), str(second), str(root), ready_child, proceed_child),
    )
    process.start()
    try:
        assert ready_parent.recv()
        prepared = parent.prepare(_item(first), (root,))
        parent.finish_operation(prepared)
        proceed_parent.send(True)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()

    reopened = removals.Store.open(target)
    assert {intent.path for intent in reopened.records} == {first, second}


def test_concurrent_writer_waits_for_lock_then_rebases(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    first = root / "first.png"
    second = root / "second.png"
    root.mkdir()
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    target = tmp_path / "pending.json"
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe()
    proceed_parent, proceed_child = context.Pipe()
    process = context.Process(
        target=_hold_lock_then_write,
        args=(str(target), str(second), str(root), ready_child, proceed_child),
    )
    process.start()
    try:
        assert ready_parent.recv()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(removals.Store.open(target).prepare, _item(first), (root,))
            proceed_parent.send(True)
            assert future.result(timeout=5).path == first
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join()

    assert {intent.path for intent in removals.Store.open(target).records} == {first, second}


def test_malformed_or_oversized_entries_fault_the_bounded_journal(tmp_path: Path) -> None:
    target = tmp_path / "pending.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "removals": [
                    {
                        "identity": "still:relative.png",
                        "path": "relative.png",
                        "kind": "still",
                        "roots": [str(tmp_path)],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert removals.Store.open(target).fault is not None
