"""Undoing a test's environment changes can never reach the real profile.

On 2026-10-01 a test called ``monkeypatch.undo()``, which also undid
conftest's per-test XDG isolation, in a run started from a developer shell.
Its next compile resolved ``paths.runtime_config_path()`` to the real
profile. conftest now makes a session-private temporary root the process's
baseline environment, so every restore lands there.

None of these tests undoes conftest's isolation itself. What a restore
returns to is the baseline, which a module-scoped fixture sees (it runs
before the per-test isolation), and a child process started from a stand-in
developer shell imports conftest and then restores with a fresh
``MonkeyPatch().undo()`` and a ``MonkeyPatch.context()`` exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

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
#: Everything the session baseline sets or drops.
WATCHED: Final = (
    *conftest.SESSION_LAYOUT,
    *conftest.SESSION_UNSET,
    *conftest.SESSION_SET,
    "WAYLAND_DISPLAY",
    conftest.TEST_WAYLAND_ENV,
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SYSTEM_BUS_ADDRESS",
)
_BASELINE: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def session_baseline() -> None:
    """The environment between tests: what undoing a test's isolation returns to.

    Module-scoped, so it runs before conftest's per-test isolation is set up.
    """
    _BASELINE["environ"] = {name: os.environ.get(name) for name in WATCHED}
    _BASELINE["resolved"] = {name: str(resolve()) for name, resolve in RESOLVERS.items()}


def _assert_sandboxed(
    root: Path, seen: dict[str, Any], *, wayland: str | None, outside: Path | None = None
) -> None:
    """``seen`` (``environ`` and ``resolved``) is the session sandbox below ``root``.

    ``wayland`` is the compositor socket that process accepted, if any: the
    only Wayland display its baseline may name.
    """
    for name, resolved in seen["resolved"].items():
        assert Path(resolved).is_relative_to(root), (name, resolved)
        # Not the real home itself: in the Nix sandbox the build user's home is
        # /build, which also holds the temporary directory.
        for guarded in conftest.GUARDED_ROOTS:
            assert not Path(resolved).is_relative_to(guarded), (name, resolved)
        if outside is not None:
            assert not Path(resolved).is_relative_to(outside), (name, resolved)
    environ = seen["environ"]
    for variable, directory in conftest.SESSION_LAYOUT.items():
        assert environ[variable] == str(root / directory), variable
    for variable in conftest.SESSION_UNSET:
        assert environ[variable] is None, variable
    for variable, value in conftest.SESSION_SET.items():
        assert environ[variable] == value, variable
    assert environ["WAYLAND_DISPLAY"] == wayland
    assert environ[conftest.TEST_WAYLAND_ENV] == wayland
    runtime = root / conftest.SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    for variable, dead in (
        ("DBUS_SESSION_BUS_ADDRESS", conftest.DEAD_SESSION_BUS),
        ("DBUS_SYSTEM_BUS_ADDRESS", conftest.DEAD_SYSTEM_BUS),
    ):
        assert environ[variable] == f"unix:path={runtime / dead}", "a dead address, never a bus"
        assert not (runtime / dead).exists(), variable


def test_the_session_baseline_is_the_sandbox() -> None:
    """What ``monkeypatch.undo()`` would restore, seen without calling it."""
    root = Path(os.environ[conftest.SESSION_ROOT_ENV])
    assert root == conftest.SESSION_ROOT
    _assert_sandboxed(root, _BASELINE, wayland=conftest.TEST_WAYLAND_SOCKET)


#: A process started from a developer shell: it imports conftest, as pytest
#: does first, then restores the environment twice and reports what it saw.
RESTORING_CHILD: Final = """
import json, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tests import conftest  # the session baseline is set up here

import pytest
from wall_in_one import paths

names, watched = json.loads(sys.argv[2]), json.loads(sys.argv[3])


def seen():
    resolved = {name: str(getattr(paths, name)()) for name in names if name != "home"}
    return {
        "environ": {name: os.environ.get(name) for name in watched},
        "resolved": {"home": str(Path.home()), **resolved},
    }


report = {"root": str(conftest.SESSION_ROOT), "baseline": seen()}
patch = pytest.MonkeyPatch()
patch.setenv("HOME", "/nonexistent/home")
patch.setenv("XDG_STATE_HOME", "/nonexistent/state")
patch.delenv("XDG_CONFIG_HOME")
patch.delenv("DBUS_SESSION_BUS_ADDRESS")
patch.undo()
report["after MonkeyPatch().undo()"] = seen()
with pytest.MonkeyPatch.context() as context:
    context.setenv("XDG_RUNTIME_DIR", "/nonexistent/run")
    context.setenv("WAYLAND_DISPLAY", "wayland-1")
report["after a MonkeyPatch.context() exit"] = seen()
print(json.dumps(report))
"""


def test_from_a_developer_shell_every_restore_lands_in_the_session_sandbox(
    tmp_path: Path,
) -> None:
    """The incident's setting: a bare pytest from a shell pointing at a real profile.

    The stand-in shell's HOME, XDG directories, buses, Wayland, niri and X11
    addresses are what a desktop session exports. After conftest's import, a
    fresh ``MonkeyPatch().undo()`` and a context exit both land in the
    child's session sandbox, and nothing is written in the stand-in.
    """
    shell = tmp_path / "developer"
    home = shell / "home"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (*WATCHED, conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_RUNTIME_DIR=str(shell / "run"),
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={shell / 'run' / 'bus'}",
        DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={shell / 'system_bus_socket'}",
        WAYLAND_DISPLAY="wayland-1",
        WAYLAND_SOCKET="9",
        NIRI_SOCKET=str(shell / "run" / "niri.sock"),
        DISPLAY=":0",
    )
    for variable in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        Path(environment[variable]).mkdir(parents=True, exist_ok=True)
    (shell / "run").mkdir(mode=0o700)
    repository = Path(conftest.__file__).resolve().parents[1]

    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            RESTORING_CHILD,
            str(repository),
            json.dumps(list(RESOLVERS)),
            json.dumps(list(WATCHED)),
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    root = Path(report.pop("root"))
    assert root.name.startswith("wio-test-session-") and not root.is_relative_to(shell)
    assert root != conftest.SESSION_ROOT, "the child's own session, not this one"
    assert list(report) == [
        "baseline",
        "after MonkeyPatch().undo()",
        "after a MonkeyPatch.context() exit",
    ]
    for moment, seen in report.items():
        try:
            _assert_sandboxed(root, seen, wayland=None, outside=shell)
        except AssertionError as error:
            raise AssertionError(f"{moment}: {error}") from error
    assert [path for path in shell.rglob("*") if not path.is_dir()] == [], "nothing written there"
    assert not root.exists(), "the session root goes with its process"


def test_the_session_runtime_directory_is_private() -> None:
    sandbox_runtime = conftest.SESSION_ROOT / conftest.SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    assert stat.S_IMODE(sandbox_runtime.stat().st_mode) == 0o700
    assert stat.S_IMODE(conftest.SESSION_ROOT.stat().st_mode) == 0o700


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


def test_noctalias_configuration_is_guarded_and_watched(tmp_path: Path) -> None:
    """Sweep 3 S-5: ~/.config/noctalia was neither guarded nor watched.

    On a stand-in home, never the real one: the tripwire sees a write there,
    and the in-process guard, given the same roots below the stand-in,
    refuses one before it happens.
    """
    assert ".config/noctalia" in conftest.REAL_PROFILE_DIRECTORIES
    assert str(conftest.REAL_HOME / ".config" / "noctalia") in conftest.GUARDED_ROOTS
    home = tmp_path / "stand-in"
    config = home / ".config" / "noctalia"
    (config / "plugins" / "wall-in-one").mkdir(parents=True)
    settings = config / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    before = conftest.real_profile_snapshot(home)

    settings.write_text('{"changed": true}\n', encoding="utf-8")
    (config / "plugins" / "wall-in-one" / "settings.json").write_text("{}", encoding="utf-8")
    assert conftest.real_profile_changes(before, conftest.real_profile_snapshot(home)) == [
        str(config / "plugins" / "wall-in-one" / "settings.json"),
        str(settings),
    ]

    stand_in_roots = [str(home / relative) for relative in conftest.REAL_PROFILE_DIRECTORIES]
    conftest.GUARDED_ROOTS.extend(stand_in_roots)
    try:
        with pytest.raises(PermissionError, match="tried to write the real profile"):
            settings.write_text("{}\n", encoding="utf-8")
        refused = list(conftest.GUARD_REFUSALS)
        assert refused == [f"open {settings}"]
    finally:
        for root in stand_in_roots:
            conftest.GUARDED_ROOTS.remove(root)
        conftest.GUARD_REFUSALS.clear()  # this test's refusal was the point
    assert settings.read_text(encoding="utf-8") == '{"changed": true}\n'


def test_the_tripwire_sees_a_child_processs_chmod(tmp_path: Path) -> None:
    """Sweep 3 S-5: a chmod leaves the mtime alone, and a child process is
    invisible to the audit hook. The snapshot now carries type and mode."""
    home = tmp_path / "stand-in"
    watched = home / ".config" / "wall-in-one"
    watched.mkdir(parents=True, mode=0o700)
    settings = watched / "settings.toml"
    settings.write_text("opacity = 0.9\n", encoding="utf-8")
    settings.chmod(0o600)
    before = conftest.real_profile_snapshot(home)

    for path, mode in ((settings, 0o644), (watched, 0o755)):
        subprocess.run(
            (
                sys.executable,
                "-c",
                "import os, sys; os.chmod(sys.argv[1], int(sys.argv[2], 8))",
                str(path),
                f"{mode:o}",
            ),
            check=True,
            timeout=30,
        )

    assert stat.S_IMODE(settings.stat().st_mode) == 0o644
    assert conftest.real_profile_changes(before, conftest.real_profile_snapshot(home)) == [
        str(watched),
        str(settings),
    ]


# -- the display rules: which compositor socket a run may draw on ---------------------


def test_a_test_not_marked_gui_has_no_display_at_all() -> None:
    for variable in ("WAYLAND_DISPLAY", conftest.TEST_WAYLAND_ENV, *conftest.SESSION_UNSET):
        assert variable not in os.environ, variable
    assert os.environ["GDK_BACKEND"] == "wayland", "and no other backend to fall back to"


def test_the_desktops_runtime_directories_are_known(tmp_path: Path) -> None:
    """``/run/user/<uid>``, the runtime directory the run started with, and
    those a parent process's conftest recorded (a child starts with that
    parent's sandbox as its ``XDG_RUNTIME_DIR``)."""
    recorded = tmp_path / "recorded"
    environ = {
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "WIO_TEST_LIVE_RUNTIME_DIRS": os.pathsep.join((str(recorded), "/run/user/4242")),
    }
    assert conftest.live_runtime_dirs(environ, 4242) == (
        Path("/run/user/4242"),
        recorded,
        tmp_path / "run",
    )
    assert conftest.live_runtime_dirs({"XDG_RUNTIME_DIR": "relative"}, 7) == (Path("/run/user/7"),)


def _listening(path: Path) -> socket.socket:
    """A Unix socket bound and listening at ``path``; nothing connects to it here."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    return listener


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """A stand-in desktop: its runtime directory with its compositor's and niri's
    sockets, and beside it a run's own compositor directory."""
    run = tmp_path / "run"
    ours = tmp_path / "wl"
    elsewhere = tmp_path / "else"
    for directory in (run, ours, elsewhere):
        directory.mkdir(mode=0o700)
    places = {
        "root": tmp_path,
        "run": run,
        "elsewhere": elsewhere,
        "live": run / "wayland-1",
        "niri": run / "niri.sock",
        "ours": ours / "wayland-0",
    }
    listeners = [_listening(places[name]) for name in ("live", "niri", "ours")]
    try:
        yield places
    finally:
        for listener in listeners:
            listener.close()


def _serving(path: str) -> list[str] | None:
    """Stands in for /proc: who listens where in the stand-in desktop."""
    name = os.path.basename(os.path.realpath(path))
    return {
        "wayland-0": ["/nix/store/weston/bin/weston", "--backend=headless", "--socket=wayland-0"],
        "wayland-1": ["niri", "--session"],
        "niri.sock": ["niri", "--session"],
        "nested": ["weston", "--backend=wayland", "--socket=nested"],
    }.get(name)


def _judge(
    desktop: dict[str, Path], candidate: str | None, **inherited: str
) -> tuple[str | None, str]:
    """The answer for a run started from the stand-in desktop's terminal."""
    environ = {
        "XDG_RUNTIME_DIR": str(desktop["run"]),
        "WAYLAND_DISPLAY": "wayland-1",
        "NIRI_SOCKET": str(desktop["niri"]),
        **inherited,
    }
    if candidate is not None:
        environ[conftest.TEST_WAYLAND_ENV] = candidate
    live = conftest.live_runtime_dirs(environ, os.getuid())
    return conftest.wayland_test_socket(environ, os.getuid(), live, _serving)


def test_the_runs_own_compositor_is_taken(desktop: dict[str, Path]) -> None:
    ours = str(desktop["ours"])
    assert _judge(desktop, ours) == (ours, "")
    # Handed down to a child as both names, as tools/headless-wayland.sh does.
    assert _judge(desktop, ours, WAYLAND_DISPLAY=ours) == (ours, "")


def _hard_link(source: Path, target: Path) -> str:
    os.link(source, target)
    return str(target)


def _symbolic_link(source: Path, target: Path) -> str:
    target.symlink_to(source)
    return str(target)


def _regular_file(path: Path) -> str:
    path.write_text("", encoding="utf-8")
    return str(path)


def _writable(directory: Path) -> Path:
    directory.chmod(0o777)
    return directory


#: A candidate made in the stand-in desktop, and the reason it is refused.
REFUSED: Final[dict[str, tuple[Callable[[dict[str, Path]], str | None], str]]] = {
    "unset": (lambda _desktop: None, "is not set"),
    "a bare name": (lambda _desktop: "wayland-0", "is not an absolute path"),
    "missing": (lambda places: str(places["elsewhere"] / "gone"), "is not there"),
    "a regular file": (
        lambda places: _regular_file(places["elsewhere"] / "file"),
        "is not a socket",
    ),
    "a symbolic link to the desktop's socket": (
        lambda places: _symbolic_link(places["live"], places["elsewhere"] / "link"),
        "is not a socket",
    ),
    "the desktop's socket": (
        lambda places: str(places["live"]),
        "in the desktop's runtime directory",
    ),
    "niri's socket": (lambda places: str(places["niri"]), "in the desktop's runtime directory"),
    "the desktop's socket through a linked directory": (
        lambda places: _symbolic_link(places["run"], places["root"] / "linked") + "/wayland-1",
        "in the desktop's runtime directory",
    ),
    "a hard link to the desktop's socket": (
        lambda places: _hard_link(places["live"], places["elsewhere"] / "wl"),
        "one of the desktop's own sockets",
    ),
    "a hard link to niri's socket": (
        lambda places: _hard_link(places["niri"], places["elsewhere"] / "wl"),
        "one of the desktop's own sockets",
    ),
    "a compositor that is not headless": (
        lambda places: _hard_link(places["ours"], places["elsewhere"] / "nested"),
        "not a headless Weston",
    ),
    "a socket no one listens on": (
        lambda places: _hard_link(places["ours"], places["elsewhere"] / "stale"),
        "no process of this account's listens on it",
    ),
    "in a directory others can write": (
        lambda places: _hard_link(places["ours"], _writable(places["elsewhere"]) / "wl"),
        "in a directory others can write",
    ),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_anything_else_is_refused(desktop: dict[str, Path], case: str) -> None:
    make, reason = REFUSED[case]
    candidate = make(desktop)
    accepted, refusal = _judge(desktop, candidate)
    assert accepted is None
    assert reason in refusal, refusal


def test_a_hard_link_to_an_inherited_wayland_display_elsewhere_is_refused(
    desktop: dict[str, Path],
) -> None:
    """A desktop whose socket is not in its runtime directory: named by path."""
    away = desktop["root"] / "away"
    away.mkdir(mode=0o700)
    listener = _listening(away / "wl")
    try:
        candidate = _hard_link(away / "wl", desktop["elsewhere"] / "wl")
        accepted, refusal = _judge(desktop, candidate, WAYLAND_DISPLAY=str(away / "wl"))
        assert accepted is None and "one of the desktop's own sockets" in refusal
        # The same socket handed down as the run's own is not the desktop's.
        handed_down = _judge(desktop, str(away / "wl"), WAYLAND_DISPLAY=str(away / "wl"))
        assert "not the desktop's" not in handed_down[1]
        assert "no process of this account's listens on it" in handed_down[1], handed_down
    finally:
        listener.close()


def test_the_listener_is_found_through_proc_without_connecting(tmp_path: Path) -> None:
    path = tmp_path / "sock"
    listener = _listening(path)
    try:
        own = Path("/proc/self/cmdline").read_bytes().split(b"\0")
        assert conftest.listening_command(str(path)) == [os.fsdecode(part) for part in own if part]
    finally:
        listener.close()
    assert conftest.listening_command(str(path)) is None, "a socket file no one listens on"
    assert conftest.listening_command(str(tmp_path / "never")) is None
