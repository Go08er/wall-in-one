"""Undoing a test's environment changes can never reach the real profile.

On 2026-10-01 a test called ``monkeypatch.undo()``, which also undid
conftest's per-test XDG isolation, in a run started from a developer shell.
Its next compile resolved ``paths.runtime_config_path()`` to the real
profile. conftest now makes a session-private temporary root the process's
baseline environment, so every restore lands there.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import conftest
from wall_in_one import paths

RESOLVERS: dict[str, Callable[[], Path]] = {
    "home": Path.home,
    "config_home": paths.config_home,
    "state_home": paths.state_home,
    "cache_home": paths.cache_home,
    "data_home": paths.data_home,
    "runtime_dir": paths.runtime_dir,
    "service_runtime_dir": paths.service_runtime_dir,
    "app_config_dir": paths.app_config_dir,
    "app_state_dir": paths.app_state_dir,
    "runtime_config_path": paths.runtime_config_path,
    "runtime_socket_path": paths.runtime_socket_path,
}


def _session_root() -> Path:
    root = Path(os.environ[conftest.SESSION_ROOT_ENV])
    assert root == conftest.SESSION_ROOT
    return root


def _assert_in_the_session_sandbox() -> None:
    root = _session_root()
    for name, resolve in RESOLVERS.items():
        resolved = resolve()
        assert resolved.is_relative_to(root), (name, resolved)
        # Not the real home itself: in the Nix sandbox the build user's home is
        # /build, which also holds the temporary directory.
        for guarded in conftest.GUARDED_ROOTS:
            assert not resolved.is_relative_to(guarded), (name, resolved)
    for variable in conftest.SESSION_UNSET:
        assert variable not in os.environ, variable
    bus = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    assert bus.startswith("unix:path=")
    dead = Path(bus.removeprefix("unix:path="))
    assert dead.is_relative_to(root) and not dead.exists(), "a dead address, never the real bus"


def test_undoing_the_tests_own_monkeypatch_lands_in_the_session_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The incident's exact move: it drops conftest's per-test isolation too."""
    assert paths.app_state_dir().is_relative_to(tmp_path), "per-test isolation is on"
    monkeypatch.undo()
    _assert_in_the_session_sandbox()


def test_a_fresh_monkeypatch_or_context_restores_into_the_session_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()  # back to the session baseline first
    elsewhere = pytest.MonkeyPatch()
    elsewhere.setenv("XDG_STATE_HOME", "/nonexistent/state")
    elsewhere.delenv("XDG_CONFIG_HOME")
    elsewhere.undo()
    _assert_in_the_session_sandbox()
    with pytest.MonkeyPatch.context() as context:
        context.setenv("HOME", "/nonexistent/home")
    _assert_in_the_session_sandbox()


def test_the_session_runtime_directory_is_private() -> None:
    sandbox_runtime = _session_root() / conftest.SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    assert stat.S_IMODE(sandbox_runtime.stat().st_mode) == 0o700
    assert stat.S_IMODE(_session_root().stat().st_mode) == 0o700


@pytest.fixture
def guarded_stand_in(tmp_path: Path) -> Iterator[Path]:
    """A temporary root the in-process guard treats as the real profile."""
    root = tmp_path / "guarded"
    root.mkdir()
    (root / "runtime.toml").write_text("before\n", encoding="utf-8")
    conftest.GUARDED_ROOTS.append(str(root))
    try:
        yield root
    finally:
        conftest.GUARDED_ROOTS.remove(str(root))
        conftest.GUARD_REFUSALS.clear()  # this test's refusals were the point


def test_the_guard_refuses_every_kind_of_write_before_it_happens(
    guarded_stand_in: Path, tmp_path: Path
) -> None:
    target = guarded_stand_in / "runtime.toml"
    outside = tmp_path / "outside.toml"
    outside.write_text("outside\n", encoding="utf-8")
    directory = os.open(guarded_stand_in, os.O_RDONLY | os.O_DIRECTORY)
    attempts: dict[str, Callable[[], object]] = {
        "open for writing": lambda: target.open("w"),
        "append": lambda: target.open("a"),
        "os.open O_CREAT": lambda: os.open(guarded_stand_in / "new", os.O_WRONLY | os.O_CREAT),
        "mkstemp, as runtime_config writes": lambda: tempfile.mkstemp(dir=guarded_stand_in),
        "os.replace into it": lambda: os.replace(outside, target),
        "os.rename out of it": lambda: os.rename(target, tmp_path / "taken.toml"),
        "unlink": target.unlink,
        "mkdir": lambda: (guarded_stand_in / "made").mkdir(),
        "utime": lambda: os.utime(target, ns=(1, 1)),
        "unlink relative to a directory descriptor": lambda: os.unlink(
            "runtime.toml", dir_fd=directory
        ),
        "shutil.rmtree": lambda: shutil.rmtree(guarded_stand_in),
    }
    try:
        for name, attempt in attempts.items():
            with pytest.raises(PermissionError, match="tried to write the real profile"):
                attempt()
            assert target.read_text(encoding="utf-8") == "before\n", name
    finally:
        os.close(directory)
    assert sorted(entry.name for entry in guarded_stand_in.iterdir()) == ["runtime.toml"]
    assert len(conftest.GUARD_REFUSALS) == len(attempts)
    # Reading is not writing.
    assert target.read_text(encoding="utf-8") == "before\n"
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_the_guard_reports_a_refusal_the_code_swallowed(guarded_stand_in: Path) -> None:
    """App code often catches OSError and carries on; the tripwire still reports it."""
    with contextlib.suppress(OSError):
        (guarded_stand_in / "runtime.toml").write_text("after\n", encoding="utf-8")
    assert conftest._escapes() == [f"refused: open {guarded_stand_in / 'runtime.toml'}"]
    assert conftest._escapes() == [], "each escape is reported once"


def test_the_tripwire_sees_a_change_in_a_watched_directory(tmp_path: Path) -> None:
    """Exercised on a stand-in home, never the real one."""
    home = tmp_path / "stand-in"
    state = home / ".local" / "state" / "wall-in-one"
    state.mkdir(parents=True)
    (state / "runtime.toml").write_text("before\n", encoding="utf-8")
    before = conftest.real_profile_snapshot(home)
    assert conftest.real_profile_changes(before, conftest.real_profile_snapshot(home)) == []

    (state / "runtime-overrides.toml").write_text("new\n", encoding="utf-8")
    os.utime(state / "runtime.toml", ns=(1, 1))
    changed = conftest.real_profile_changes(before, conftest.real_profile_snapshot(home))
    assert changed == [str(state / "runtime-overrides.toml"), str(state / "runtime.toml")]

    (home / ".config" / "wall-in-one").mkdir(parents=True)
    appeared = conftest.real_profile_changes(before, conftest.real_profile_snapshot(home))
    assert str(home / ".config" / "wall-in-one") in appeared


def test_the_tripwire_skips_only_what_the_desktop_writes_by_itself(tmp_path: Path) -> None:
    home = tmp_path / "stand-in"
    noctalia = home / ".local" / "state" / "noctalia"
    (noctalia / "community-templates" / "bat").mkdir(parents=True)
    state = home / ".local" / "state" / "wall-in-one"
    state.mkdir(parents=True)
    before = conftest.real_profile_snapshot(home)
    (noctalia / "notification_history.json").write_text("[]", encoding="utf-8")
    (noctalia / "community-templates" / "bat" / ".noctalia-cache.json").write_text("{}")
    (noctalia / "clipboard" / "entries").mkdir(parents=True)
    (noctalia / "settings.toml").write_text("[wallpaper]\n", encoding="utf-8")
    (state / "palette.json").write_text("{}", encoding="utf-8")
    assert conftest.real_profile_changes(before, conftest.real_profile_snapshot(home)) == []
    (state / "playlists.json").write_text("{}", encoding="utf-8")
    backup = noctalia / "settings.toml.bak-wall-in-one-20261001"
    backup.write_text("[wallpaper]\n", encoding="utf-8")
    (noctalia / ".settings.toml.backup-x1").write_text("", encoding="utf-8")
    assert conftest.real_profile_changes(before, conftest.real_profile_snapshot(home)) == [
        str(noctalia / ".settings.toml.backup-x1"),
        str(backup),
        str(state / "playlists.json"),
    ]
