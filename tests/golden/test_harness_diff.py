"""The write gate's own diff: an allowed write never hides a chmod.

Every golden check judges :func:`harness.diff`'s changes against allowances
that name their kinds. ``diff`` once reported one kind per path, preferring a
content change, so a file both rewritten and chmodded passed as an allowed
write. The VM's upgrade-rollback gate shares the same ``diff``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.golden import harness
from tests.golden.harness import Allowance

NAME = "settings.toml"


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    target = home / NAME
    target.write_text("key = 1\n")
    target.chmod(0o600)
    return home, target


def _any_write() -> list[Allowance]:
    return [Allowance(NAME, frozenset({"modified", "rewritten"}), "an allowed write")]


def _kinds(changes: list[harness.Change]) -> list[tuple[str, str]]:
    return [(change.path, change.kind) for change in changes]


def test_a_chmod_with_an_allowed_content_change_is_still_a_finding(tmp_path: Path) -> None:
    home, target = _home(tmp_path)
    before = harness.snapshot(home)
    target.write_text("key = 2\n")
    target.chmod(0o666)

    changes = harness.diff(before, harness.snapshot(home))

    assert _kinds(changes) == [(NAME, "modified"), (NAME, "mode")]
    with pytest.raises(AssertionError, match=r"mode: settings\.toml \(600 -> 666\)"):
        harness.check_changes(changes, _any_write())


def test_a_chmod_with_an_allowed_rewrite_is_still_a_finding(tmp_path: Path) -> None:
    home, target = _home(tmp_path)
    before = harness.snapshot(home)
    replacement = home / f".{NAME}.tmp"
    replacement.write_bytes(target.read_bytes())
    replacement.chmod(0o666)
    os.replace(replacement, target)

    changes = harness.diff(before, harness.snapshot(home))

    assert _kinds(changes) == [(NAME, "rewritten"), (NAME, "mode")]
    with pytest.raises(AssertionError, match=r"mode: settings\.toml \(600 -> 666\)"):
        harness.check_changes(changes, _any_write())


def test_an_allowed_write_that_keeps_the_mode_is_one_change(tmp_path: Path) -> None:
    home, target = _home(tmp_path)
    before = harness.snapshot(home)
    target.write_text("key = 2\n")

    changes = harness.diff(before, harness.snapshot(home))

    assert _kinds(changes) == [(NAME, "modified")]
    harness.check_changes(changes, _any_write())


def test_a_chmod_alone_is_one_mode_change(tmp_path: Path) -> None:
    home, target = _home(tmp_path)
    before = harness.snapshot(home)
    target.chmod(0o644)

    assert _kinds(harness.diff(before, harness.snapshot(home))) == [(NAME, "mode")]
