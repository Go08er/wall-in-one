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
