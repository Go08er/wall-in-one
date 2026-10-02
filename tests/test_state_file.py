"""Durability primitives shared by settings and every authoring store."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from wall_in_one import config, file_io
from wall_in_one.library import displays, favourites, pairings, playlists, schedules, state_file


@pytest.mark.parametrize(
    "document",
    (
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":"\\ud800"}',
        b'{"\\udfff":"value"}',
    ),
)
def test_read_object_rejects_nonfinite_and_non_utf8_json_values(
    tmp_path: Path,
    document: bytes,
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(document)

    parsed, fault = state_file.read_object(
        target,
        maximum_bytes=1024,
        description="test state",
    )

    assert parsed is None
    assert fault == "state.json is not readable JSON"


def test_read_object_converts_canonicalization_value_error_to_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"value":1}', encoding="utf-8")

    def fail_canonicalization(*_args: object, **_kwargs: object) -> str:
        raise ValueError("injected canonicalization failure")

    monkeypatch.setattr(json, "dumps", fail_canonicalization)

    parsed, fault = state_file.read_object(
        target,
        maximum_bytes=1024,
        description="test state",
    )

    assert parsed is None
    assert fault == "state.json is not readable JSON"


def test_read_snapshots_bind_nested_store_reads_to_exact_bytes_and_absence(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"generation":"public"}', encoding="utf-8")

    with state_file.read_snapshots({target: b'{"generation":"retained"}'}):
        retained, retained_fault = state_file.read_object(
            target,
            maximum_bytes=1024,
            description="test state",
        )
    with state_file.read_snapshots({target: None}):
        absent, absent_fault = state_file.read_object(
            target,
            maximum_bytes=1024,
            description="test state",
        )
    public, public_fault = state_file.read_object(
        target,
        maximum_bytes=1024,
        description="test state",
    )

    assert retained == {"generation": "retained"}
    assert retained_fault is None
    assert absent is None and absent_fault is None
    assert public == {"generation": "public"}
    assert public_fault is None


def test_preserving_a_fault_never_replaces_an_older_recovery_copy(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("newly broken", encoding="utf-8")
    first = tmp_path / "state.json.broken"
    second = tmp_path / "state.json.broken.1"
    first.write_text("oldest recovery", encoding="utf-8")
    second.write_text("newer recovery", encoding="utf-8")

    backup = state_file.preserve_faulted(target)

    assert backup == tmp_path / "state.json.broken.2"
    assert backup.read_text(encoding="utf-8") == "newly broken"
    assert first.read_text(encoding="utf-8") == "oldest recovery"
    assert second.read_text(encoding="utf-8") == "newer recovery"
    assert not target.exists()


def test_an_unexpected_directory_is_not_moved_for_a_state_write(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.mkdir()

    with pytest.raises(OSError):
        state_file.preserve_faulted(target)

    assert target.is_dir()
    assert not (tmp_path / "state.json.broken").exists()


def test_preserving_a_fault_carries_the_exact_inspected_entry_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("leave me alone", encoding="utf-8")
    target.symlink_to(sentinel)
    move = file_io.atomic_move_no_replace
    observed: list[int] = []

    def observe_type(
        source: Path,
        destination: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
    ) -> None:
        observed.append(expected_file_type)
        move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
        )

    monkeypatch.setattr(file_io, "atomic_move_no_replace", observe_type)

    backup = state_file.preserve_faulted(target)

    assert observed == [stat.S_IFLNK]
    assert backup.is_symlink()
    assert backup.readlink() == sentinel
    assert sentinel.read_text(encoding="utf-8") == "leave me alone"
    assert not target.exists()


def test_preserving_a_fault_never_moves_a_same_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_text("expected broken state", encoding="utf-8")
    original = tmp_path / "original-state"
    real_rename = file_io._rename_noreplace
    raced = False

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal raced
        if source == target and not raced:
            raced = True
            source.rename(original)
            source.write_text("late replacement", encoding="utf-8")
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)

    with pytest.raises(file_io.PathChangedError):
        state_file.preserve_faulted(target)

    assert target.read_text(encoding="utf-8") == "late replacement"
    assert original.read_text(encoding="utf-8") == "expected broken state"
    assert not (tmp_path / "state.json.broken").exists()


def test_an_observed_fault_never_moves_an_in_place_same_inode_repair(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("broken", encoding="utf-8")

    with state_file.observe(target) as observed:
        identity = target.stat().st_dev, target.stat().st_ino
        repaired = '{"version": 1, "repaired": true}\n'
        target.write_text(repaired, encoding="utf-8")
        assert (target.stat().st_dev, target.stat().st_ino) == identity

        with pytest.raises(file_io.PathChangedError):
            state_file.preserve_faulted(target, observed=observed)

    assert target.read_text(encoding="utf-8") == repaired
    assert not (tmp_path / "state.json.broken").exists()


Writer = Callable[[Path], Path]


@pytest.mark.parametrize(
    "writer",
    (
        lambda path: config.save(config.Settings(), path),
        lambda path: pairings.save({}, path),
        lambda path: playlists.save({}, path),
        lambda path: schedules.save((), path),
        lambda path: displays.save({}, path),
        lambda path: favourites.save(favourites.Favourites(), path),
    ),
    ids=("settings", "pairings", "playlists", "schedules", "displays", "favourites"),
)
def test_every_atomic_writer_fsyncs_its_containing_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: Writer,
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(state_file, "fsync_parent", observed.append)
    target = tmp_path / "state"

    assert writer(target) == target
    assert observed == [target]


def test_parent_fsync_opens_the_directory_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[Path, int]] = []
    real_open = os.open

    def watch(path: Path, flags: int) -> int:
        observed.append((Path(path), flags))
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", watch)
    state_file.fsync_parent(tmp_path / "state.json")

    assert observed[0][0] == tmp_path
    assert observed[0][1] & os.O_DIRECTORY


@pytest.mark.parametrize(
    "writer",
    (
        lambda path: pairings.save({}, path),
        lambda path: playlists.save({}, path),
        lambda path: schedules.save((), path),
        lambda path: displays.save({}, path),
        lambda path: favourites.save(favourites.Favourites(), path),
    ),
    ids=("pairings", "playlists", "schedules", "displays", "favourites"),
)
def test_authoring_writers_ignore_the_predictable_legacy_temporary_symlink(
    tmp_path: Path, writer: Writer
) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("do not overwrite", encoding="utf-8")
    legacy_temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    legacy_temporary.symlink_to(sentinel)

    writer(target)

    assert target.is_file()
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"
    assert legacy_temporary.is_symlink()


def test_reentrant_atomic_writes_have_distinct_private_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    sources: list[Path] = []
    inside_reentrant_write = False
    real_replace = os.replace

    def replace(source: object, destination: object) -> None:
        nonlocal inside_reentrant_write
        sources.append(Path(source))  # type: ignore[arg-type]
        if not inside_reentrant_write:
            inside_reentrant_write = True
            state_file.write_atomic_text(target, "inner\n")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace)

    state_file.write_atomic_text(target, "outer\n")

    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert all(source.parent == target.parent for source in sources)
    assert target.read_text(encoding="utf-8") == "outer\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_replace_atomic_write_preserves_an_existing_manual_repair(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("manual repair\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert target.read_text(encoding="utf-8") == "manual repair\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_replace_atomic_write_restores_a_post_rename_manual_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    repair = tmp_path / "manual-repair.json"
    repair.write_text("manual repair\n", encoding="utf-8")
    repair_identity = file_io.path_identity(repair)
    rename = file_io._rename_noreplace
    raced = False

    def replace_after_candidate_rename(source: Path, destination: Path) -> None:
        nonlocal raced
        rename(source, destination)
        if destination == target and source.name.endswith(".tmp") and not raced:
            os.replace(repair, target)
            raced = True

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_after_candidate_rename)

    with pytest.raises(file_io.PathChangedError):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert raced
    assert file_io.path_identity(target) == repair_identity
    assert target.read_text(encoding="utf-8") == "manual repair\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_replace_atomic_write_restores_an_in_place_post_publish_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    rename = file_io._rename_noreplace
    published = False

    def edit_after_candidate_publish(source: Path, destination: Path) -> None:
        nonlocal published
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            destination.write_text("manual in-place repair\n", encoding="utf-8")
            published = True

    monkeypatch.setattr(file_io, "_rename_noreplace", edit_after_candidate_publish)

    with pytest.raises(file_io.PathChangedError, match="concurrent entry was restored"):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert published
    assert target.read_text(encoding="utf-8") == "manual in-place repair\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_replace_atomic_write_restores_an_unchanged_candidate_after_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    published = False
    fail_verification = False

    def chmod_after_candidate_publish(source: Path, destination: Path) -> None:
        nonlocal fail_verification, published
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            destination.chmod(0o640)
            published = True
            fail_verification = True

    def fail_first_post_publish_verification(path: Path) -> os.stat_result:
        nonlocal fail_verification
        if path == target and fail_verification:
            fail_verification = False
            raise OSError("injected transient verification failure")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", chmod_after_candidate_publish)
    monkeypatch.setattr(Path, "lstat", fail_first_post_publish_verification)

    with pytest.raises(file_io.PathChangedError, match="publication candidate was restored"):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert published
    assert target.read_text(encoding="utf-8") == "recovered mutation\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_replace_atomic_write_rejects_a_nonregular_replacement_during_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("leave me alone\n", encoding="utf-8")
    preserved_candidate = tmp_path / "preserved-candidate"
    rename = file_io._rename_noreplace
    pread = os.pread
    published = False
    replaced = False
    temporary: Path | None = None

    def change_mtime_after_candidate_publish(source: Path, destination: Path) -> None:
        nonlocal published
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            status = destination.stat()
            os.utime(
                destination,
                ns=(status.st_atime_ns, status.st_mtime_ns + 1),
            )
            published = True

    def replace_during_pinned_read(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal replaced, temporary
        if not replaced:
            temporary = next(tmp_path.glob(".*.tmp"))
            temporary.rename(preserved_candidate)
            temporary.symlink_to(sentinel)
            replaced = True
        return pread(descriptor, count, offset)

    monkeypatch.setattr(file_io, "_rename_noreplace", change_mtime_after_candidate_publish)
    monkeypatch.setattr(os, "pread", replace_during_pinned_read)

    with pytest.raises(file_io.PathChangedError):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert published and replaced
    assert not target.exists()
    assert preserved_candidate.read_text(encoding="utf-8") == "recovered mutation\n"
    assert temporary is not None and temporary.is_symlink()
    assert temporary.readlink() == sentinel
    assert sentinel.read_text(encoding="utf-8") == "leave me alone\n"


def test_no_replace_atomic_write_preserves_an_in_place_edit_and_newer_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    rename = file_io._rename_noreplace
    published = False
    newer_writer = False

    def edit_then_install_newer_writer(source: Path, destination: Path) -> None:
        nonlocal newer_writer, published
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            destination.write_text("manual in-place repair\n", encoding="utf-8")
            published = True
        elif published and not newer_writer and source == target:
            target.write_text("newer canonical writer\n", encoding="utf-8")
            newer_writer = True

    monkeypatch.setattr(file_io, "_rename_noreplace", edit_then_install_newer_writer)

    with pytest.raises(file_io.PathChangedError, match="newer concurrent entry remains"):
        state_file.write_atomic_text(
            target,
            "recovered mutation\n",
            replace_existing=False,
        )

    assert published and newer_writer
    assert target.read_text(encoding="utf-8") == "newer canonical writer\n"
    preserved = list(tmp_path.glob(".*.tmp"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "manual in-place repair\n"


def test_failed_atomic_write_never_discards_a_same_type_temporary_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    original = tmp_path / "original-private-temporary"
    real_lstat = Path.lstat
    temporary: Path | None = None
    expected_identity: file_io.PathIdentity | None = None
    raced = False
    real_sync = os.fsync

    def replace_then_fail_sync(descriptor: int) -> None:
        nonlocal expected_identity, raced, temporary
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            real_sync(descriptor)
            return
        temporary = next(tmp_path.glob(".*.tmp"))
        status = real_lstat(temporary)
        expected_identity = status.st_dev, status.st_ino
        temporary.rename(original)
        temporary.write_text("same-type replacement survives\n", encoding="utf-8")
        raced = True
        raise OSError("injected sync failure")

    def spoof_reused_identity(candidate: Path) -> os.stat_result:
        status = real_lstat(candidate)
        if raced and temporary is not None and candidate == temporary:
            assert expected_identity is not None
            fields = list(status)
            fields[stat.ST_DEV] = expected_identity[0]
            fields[stat.ST_INO] = expected_identity[1]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(os, "fsync", replace_then_fail_sync)
    monkeypatch.setattr(Path, "lstat", spoof_reused_identity)

    with pytest.raises(OSError, match="injected sync failure"):
        state_file.write_atomic_text(target, "candidate contents\n")

    assert temporary is not None
    assert temporary.read_text(encoding="utf-8") == "same-type replacement survives\n"
    assert original.read_text(encoding="utf-8") == "candidate contents\n"
    assert not target.exists()


def test_failed_atomic_write_never_double_closes_a_reused_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    real_discard = file_io.discard_regular_if_same
    real_sync = os.fsync
    failed_descriptor: int | None = None
    reused_descriptor: int | None = None

    def fail_sync(descriptor: int) -> None:
        nonlocal failed_descriptor
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed_descriptor = descriptor
            raise OSError("injected sync failure")
        real_sync(descriptor)

    def retain_reused_descriptor(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
    ) -> bool:
        nonlocal reused_descriptor
        reused_descriptor = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
        assert reused_descriptor == failed_descriptor
        return real_discard(
            path,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
        )

    monkeypatch.setattr(os, "fsync", fail_sync)
    monkeypatch.setattr(file_io, "discard_regular_if_same", retain_reused_descriptor)

    try:
        with pytest.raises(OSError, match="injected sync failure"):
            state_file.write_atomic_text(target, "candidate contents\n")

        assert reused_descriptor is not None
        os.fstat(reused_descriptor)
        # The same injected fsync failure also prevents the exact-inode
        # discard marker from becoming durable.  Cleanup must therefore fail
        # closed and restore the unpublished temporary instead of truncating
        # it; this test's contract is that the recycled descriptor stays open.
        preserved = list(tmp_path.glob(".*.tmp"))
        assert len(preserved) == 1
        assert preserved[0].read_text(encoding="utf-8") == "candidate contents\n"
    finally:
        if reused_descriptor is not None:
            os.close(reused_descriptor)


def test_mutation_lock_refuses_a_predictable_symlink(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("do not touch", encoding="utf-8")
    lock = target.with_name(f".{target.name}.mutation.lock")
    lock.symlink_to(sentinel)

    with pytest.raises(OSError), state_file.mutation_lock(target, description="fixture"):
        pytest.fail("a symlink must not grant the mutation lock")

    assert sentinel.read_text(encoding="utf-8") == "do not touch"


def test_nested_different_target_mutation_locks_are_same_thread_reentrant(
    tmp_path: Path,
) -> None:
    first = tmp_path / "migration-marker.json"
    second = tmp_path / "settings.json"

    with (
        state_file.mutation_lock(first, description="outer"),
        state_file.mutation_lock(second, description="inner"),
    ):
        second.write_text("saved", encoding="utf-8")

    assert second.read_text(encoding="utf-8") == "saved"


def test_marker_lock_can_exclude_migration_without_owning_every_state_gate(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "migration-marker.json"
    target = tmp_path / "playlists.json"

    with (
        state_file.mutation_lock(
            marker,
            description="migration",
            process_gate=False,
        ),
        state_file.mutation_lock(target, description="playlists"),
    ):
        target.write_text("saved", encoding="utf-8")

    assert target.read_text(encoding="utf-8") == "saved"


# -- forward compatibility: newer versions and unknown keys -------------------


@pytest.mark.parametrize("version", (2, 3, 99))
def test_an_integer_version_above_the_understood_one_is_a_newer_version_fault(
    version: int,
) -> None:
    fault = state_file.newer_version_fault(Path("/s/playlists.json"), {"version": version}, 1)

    assert fault == (
        "playlists.json was saved by a newer version of Wall-in-One "
        f"(unsupported version {version}; this version understands up to 1)"
    )


@pytest.mark.parametrize("version", (None, 0, 1, -3, True, False, "2", 2.0, [2], {"v": 2}))
def test_only_an_integer_above_the_understood_version_is_newer(version: object) -> None:
    """Damage (a string, a bool, zero, an older unknown) is never the future."""
    document = {} if version is None else {"version": version}

    assert state_file.newer_version_fault(Path("/s/x.json"), document, 1) is None


def test_the_newer_version_refusal_says_what_happened_and_what_to_do() -> None:
    assert state_file.newer_version_refusal(Path("/s/schedules.json")) == (
        "schedules.json was saved by a newer version of Wall-in-One; "
        "open that version to change it. Nothing was changed."
    )


def test_fault_kinds_distinguish_newer_from_unreadable() -> None:
    assert state_file.fault_kind(None, newer_version=False) is None
    assert state_file.fault_kind("x", newer_version=False) == state_file.UNREADABLE
    assert state_file.fault_kind("x", newer_version=True) == state_file.NEWER_VERSION
    assert state_file.NEWER_VERSION == "newer-version"
    assert state_file.Reading(1).fault_kind is None
    assert state_file.Reading(1, "bad").fault_kind == state_file.UNREADABLE
    assert state_file.Reading(1, "new", newer_version=True).fault_kind == "newer-version"


_ENTRY = state_file.Shape(known=frozenset({"id", "source"}), identity="id", strip_identity=True)
_SHAPE = state_file.Shape(
    known=frozenset({"version"}),
    records={
        "items": state_file.Shape(
            known=frozenset({"id", "name"}),
            identity="id",
            strip_identity=True,
            records={"entries": _ENTRY},
            objects={"health": state_file.Shape(known=frozenset({"state"}))},
        )
    },
)


def test_unknown_fields_are_captured_at_every_level_and_merged_back_by_identity() -> None:
    stored = {
        "version": 1,
        "written_by": "a newer build",
        "items": [
            {
                "id": " a ",
                "name": "A",
                "description": "kept",
                "entries": [{"id": "e1", "source": "/x", "weight": 3}],
                "health": {"state": "borked", "since": "yesterday"},
            },
            {"id": "b", "name": "B"},
        ],
    }
    unknown = state_file.capture_unknown(stored, _SHAPE)
    assert unknown

    # A rewrite in another order, with a renamed record and its parser's
    # trimmed identity, still finds every captured field again.
    rewritten = {
        "version": 1,
        "items": [
            {"id": "b", "name": "B"},
            {
                "id": "a",
                "name": "Renamed",
                "entries": [{"id": "e2", "source": "/y"}, {"id": "e1", "source": "/x"}],
                "health": {"state": "borked"},
            },
        ],
    }
    merged = state_file.merge_unknown(rewritten, unknown, _SHAPE)

    assert merged == {
        "version": 1,
        "items": [
            {"id": "b", "name": "B"},
            {
                "id": "a",
                "name": "Renamed",
                "entries": [
                    {"id": "e2", "source": "/y"},
                    {"id": "e1", "source": "/x", "weight": 3},
                ],
                "health": {"state": "borked", "since": "yesterday"},
                "description": "kept",
            },
        ],
        "written_by": "a newer build",
    }


def test_known_fields_are_never_carried_and_a_written_field_is_never_overwritten() -> None:
    """Clearing a modelled optional field must not resurrect the old value."""
    unknown = state_file.capture_unknown(
        {"version": 1, "items": [{"id": "a", "name": "Old", "extra": "carried"}]},
        _SHAPE,
    )
    merged = state_file.merge_unknown({"items": [{"id": "a", "extra": "new"}]}, unknown, _SHAPE)

    assert merged == {"items": [{"id": "a", "extra": "new"}]}


def test_a_removed_record_or_nested_object_takes_its_unknown_fields_with_it() -> None:
    unknown = state_file.capture_unknown(
        {
            "items": [
                {"id": "gone", "name": "G", "note": "x"},
                {"id": "kept", "name": "K", "health": {"state": "borked", "since": 1}},
            ]
        },
        _SHAPE,
    )

    merged = state_file.merge_unknown({"items": [{"id": "kept", "name": "K"}]}, unknown, _SHAPE)

    assert merged == {"items": [{"id": "kept", "name": "K"}]}


def test_a_duplicate_identity_carries_only_the_first_records_fields() -> None:
    unknown = state_file.capture_unknown(
        {"items": [{"id": "a", "note": "first"}, {"id": "a", "note": "second"}]}, _SHAPE
    )

    merged = state_file.merge_unknown({"items": [{"id": "a"}]}, unknown, _SHAPE)

    assert merged == {"items": [{"id": "a", "note": "first"}]}


def test_a_document_with_nothing_unknown_captures_nothing() -> None:
    unknown = state_file.capture_unknown(
        {"version": 1, "items": [{"id": "a", "name": "A", "entries": [], "health": {}}]},
        _SHAPE,
    )

    assert not unknown
    assert unknown is state_file.NOTHING_UNKNOWN
    assert state_file.merge_unknown({"version": 1}, unknown, _SHAPE) == {"version": 1}


# -- format-change guards: lazy bump on use, one backup before the first bump ---------

FORMATS = state_file.FormatVersions(oldest=1, floor=2, current=3)


def test_format_versions_must_be_ordered() -> None:
    for oldest, floor, current in ((0, 1, 1), (2, 1, 3), (1, 3, 2)):
        with pytest.raises(ValueError):
            state_file.FormatVersions(oldest=oldest, floor=floor, current=current)


@pytest.mark.parametrize(
    ("document", "declared"),
    (
        ({}, 1),
        ({"version": 1}, 1),
        ({"version": 2}, 2),
        ({"version": 3}, 3),
        ({"version": 4}, None),
        ({"version": 0}, None),
        ({"version": "2"}, None),
        ({"version": True}, None),
        ({"version": 2.0}, None),
    ),
)
def test_a_declared_version_is_one_this_build_reads_or_none(
    document: dict[str, object], declared: int | None
) -> None:
    assert FORMATS.declared(document) == declared


@pytest.mark.parametrize(
    ("on_disk", "required", "written"),
    (
        # No file, or one too damaged to say: the oldest format the data fits.
        (None, 2, 2),
        (None, 3, 3),
        # Lazy bump on use: the file keeps its version unless the data needs more.
        (2, 2, 2),
        (2, 3, 3),
        # The old v1 -> v2 upgrade is unchanged; v1 straight to v3 when needed.
        (1, 2, 2),
        (1, 3, 3),
        # Never back down: a bumped file stays bumped.
        (3, 2, 3),
        (3, 3, 3),
    ),
)
def test_the_version_written_keeps_the_files_unless_the_data_needs_more(
    on_disk: int | None, required: int, written: int
) -> None:
    assert FORMATS.to_write(on_disk, required) == written


@pytest.mark.parametrize(("on_disk", "required"), ((None, 1), (None, 4), (4, 2), (0, 2)))
def test_versions_outside_the_declared_range_are_a_programming_error(
    on_disk: int | None, required: int
) -> None:
    with pytest.raises(ValueError):
        FORMATS.to_write(on_disk, required)


@pytest.mark.parametrize(
    ("on_disk", "writing", "bump"),
    (
        (None, 2, False),
        (None, 3, False),
        (1, 2, False),
        (2, 2, False),
        (3, 3, False),
        (2, 3, True),
        (1, 3, True),
    ),
)
def test_only_a_move_above_the_floor_and_the_files_version_is_a_bump(
    on_disk: int | None, writing: int, bump: bool
) -> None:
    assert FORMATS.is_bump(on_disk, writing) is bump


def test_the_backup_is_named_after_the_version_it_keeps(tmp_path: Path) -> None:
    assert state_file.version_backup_path(tmp_path / "schedules.json", 2) == (
        tmp_path / "schedules.json.v2-backup"
    )


def _back_up(target: Path, replaced: int = 2) -> Path:
    with state_file.observe(target) as observed:
        return state_file.backup_before_bump(target, observed=observed, replaced=replaced)


def test_a_backup_keeps_the_exact_bytes_and_mode_and_leaves_the_original(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    original = b'{"version": 2,\n  "rules": []}  \n\xc3\xa9'
    target.write_bytes(original)
    target.chmod(0o640)

    backup = _back_up(target)

    assert backup == tmp_path / "schedules.json.v2-backup"
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    assert target.read_bytes() == original
    assert backup.stat().st_ino != target.stat().st_ino
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "schedules.json",
        "schedules.json.v2-backup",
    ]


def test_an_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    backup = tmp_path / "schedules.json.v2-backup"
    backup.write_bytes(b"the first copy")
    backup.chmod(0o600)
    before = backup.stat()
    target.write_bytes(b"a later generation")

    assert _back_up(target) == backup

    assert backup.read_bytes() == b"the first copy"
    assert file_io.file_fingerprint(backup.stat()) == file_io.file_fingerprint(before)


def test_a_backup_written_by_someone_else_first_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-replace publication loses the race politely: theirs stays."""
    target = tmp_path / "schedules.json"
    target.write_bytes(b"ours")
    backup = tmp_path / "schedules.json.v2-backup"
    real = state_file.write_atomic_bytes

    def raced(
        path: Path, contents: bytes, *, replace_existing: bool = True, mode: int | None = None
    ) -> None:
        backup.write_bytes(b"theirs")
        real(path, contents, replace_existing=replace_existing, mode=mode)

    monkeypatch.setattr(state_file, "write_atomic_bytes", raced)

    assert _back_up(target) == backup
    assert backup.read_bytes() == b"theirs"
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


@pytest.mark.parametrize("occupant", ("directory", "symlink"))
def test_a_backup_name_taken_by_something_else_refuses(tmp_path: Path, occupant: str) -> None:
    target = tmp_path / "schedules.json"
    target.write_bytes(b"{}")
    backup = tmp_path / "schedules.json.v2-backup"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"not mine")
    if occupant == "directory":
        backup.mkdir()
    else:
        backup.symlink_to(elsewhere)

    with pytest.raises(OSError, match="not a regular file"):
        _back_up(target)

    assert elsewhere.read_bytes() == b"not mine"
    assert target.read_bytes() == b"{}"


def test_a_file_changed_since_it_was_observed_is_not_backed_up(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_bytes(b"what was read")
    with state_file.observe(target) as observed:
        target.write_bytes(b"an editor's later save")
        with pytest.raises(file_io.PathChangedError):
            state_file.backup_before_bump(target, observed=observed, replaced=2)

    assert not (tmp_path / "schedules.json.v2-backup").exists()


def test_an_absent_file_has_nothing_to_back_up(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    with pytest.raises(OSError):
        _back_up(target)
    assert not (tmp_path / "schedules.json.v2-backup").exists()


def test_bytes_and_text_writers_publish_the_same_file(tmp_path: Path) -> None:
    text = '{"name": "Frösche"}\n'
    state_file.write_atomic_text(tmp_path / "a.json", text)
    state_file.write_atomic_bytes(tmp_path / "b.json", text.encode())
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    with pytest.raises(FileExistsError):
        state_file.write_atomic_bytes(tmp_path / "a.json", b"x", replace_existing=False)
    assert (tmp_path / "a.json").read_text() == text


# -- a failure after publication is not a refusal -------------------------------------


def _sync_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(_path: Path) -> None:
        raise OSError(5, "injected post-replace directory sync failure")

    monkeypatch.setattr(state_file, "fsync_parent", fails)


def test_a_folder_sync_failure_after_the_replace_says_the_file_was_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "store.json"
    target.write_text("old")
    _sync_fails(monkeypatch)

    with pytest.raises(state_file.PublishedNotDurableError, match="was saved") as raised:
        state_file.write_atomic_text(target, "new")

    assert raised.value.path == target
    assert target.read_text() == "new"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["store.json"]


def test_settings_published_before_a_sync_failure_are_reported_as_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's B-2 at the config layer: opacity 0.4 → 0.8, then the sync fails."""
    target = config.save(config.Settings(roots=(tmp_path,), opacity=0.4))
    _sync_fails(monkeypatch)

    with pytest.raises(config.SettingsNotDurableError) as raised:
        config.update({"opacity": 0.8})

    assert raised.value.settings.opacity == 0.8
    assert config.load_strict(target).opacity == 0.8
