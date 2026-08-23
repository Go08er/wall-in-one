"""Durability primitives shared by settings and every authoring store."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import displays, favourites, pairings, playlists, schedules, state_file


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


def test_mutation_lock_refuses_a_predictable_symlink(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("do not touch", encoding="utf-8")
    lock = target.with_name(f".{target.name}.mutation.lock")
    lock.symlink_to(sentinel)

    with pytest.raises(OSError), state_file.mutation_lock(target, description="fixture"):
        pytest.fail("a symlink must not grant the mutation lock")

    assert sentinel.read_text(encoding="utf-8") == "do not touch"
