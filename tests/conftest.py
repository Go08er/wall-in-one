"""Guards that apply to every test, whether or not it remembers to ask.

The suite drives an app whose whole job is changing the machine it runs on.
Individual tests have always patched the calls they knew about -- but "the
calls they knew about" is exactly the thing that goes stale, and it did:
step 10 made `session._apply` set the colour scheme as well as the wallpaper,
`tests/test_session.py` patched only `set_wallpaper`, and running the suite
quietly repainted the developer's desktop with the default generator.

So the rule is inverted here. Every call that changes Noctalia's state is
refused by default, at the module boundary, for every test in the suite. A
test that means to exercise one patches it, which is what the existing tests
already do; a test that does not mean to gets an `AssertionError` naming the
call instead of silently reaching out of the sandbox.

`noctalia msg` is a subprocess, so nothing here can be enforced by types or by
a fixture a test forgets to request. It has to be autouse and it has to be
here.

The per-test isolation below is not enough on its own: it is a monkeypatch,
and undoing it (``monkeypatch.undo()``, a context exit, any env restore)
returned the process to whatever environment pytest started with. On
2026-10-01 a test did exactly that in a run started from a developer shell,
and its next compile replaced the real ``runtime.toml``. So the baseline
itself is a sandbox: before anything else is imported, this module points
``HOME`` and every XDG directory at a session-private temporary root, gives
the session and system buses dead addresses there, tells GTK not to use
portals, and drops the Wayland and niri addresses -- all before any test
module imports or initializes GTK, whose module-scoped setup runs before the
per-test guard below. Restoring the environment
can then only ever land there. Below that, an audit hook refuses any write
this process attempts under the real profile directories, and a tripwire
fails the test after which they were written or changed.
"""

from __future__ import annotations

import atexit
import fnmatch
import os
import pwd
import shutil
import socket
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

SESSION_ROOT_ENV: Final = "WIO_TEST_SESSION_ROOT"
_SESSION_OWNER_ENV: Final = "WIO_TEST_SESSION_PID"
#: Environment variable -> directory below the session root.
SESSION_LAYOUT: Final = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "config",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_DATA_HOME": "data",
    "XDG_RUNTIME_DIR": "run",
}
#: Addresses of the live desktop. DISPLAY stays: GUI tests run under Xvfb.
SESSION_UNSET: Final = ("WAYLAND_DISPLAY", "NIRI_SOCKET")
#: The session bus gets a dead address below the session root rather than
#: none: with DISPLAY set, an unset address makes GTK autolaunch a private
#: dbus-daemon (and document portals) that outlive the run. The system bus
#: gets one too: unset, it means the machine's real one.
DEAD_SESSION_BUS: Final = "no-session-bus"
DEAD_SYSTEM_BUS: Final = "no-system-bus"
#: Portals would need that bus anyway; GTK is told not to look for them.
SESSION_SET: Final = {"GDK_DEBUG": "no-portals", "GTK_USE_PORTAL": "0"}


def _enter_session_sandbox() -> Path:
    """Make a private temporary root the whole process's baseline environment."""
    existing = os.environ.get(SESSION_ROOT_ENV)
    if existing and os.environ.get(_SESSION_OWNER_ENV) == str(os.getpid()):
        root = Path(existing)  # this module imported twice in one process
    else:
        root = Path(tempfile.mkdtemp(prefix="wio-test-session-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    os.chmod(root, 0o700)
    for variable, name in SESSION_LAYOUT.items():
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        os.environ[variable] = str(directory)
    for variable in SESSION_UNSET:
        os.environ.pop(variable, None)
    runtime = root / SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / DEAD_SESSION_BUS}"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={runtime / DEAD_SYSTEM_BUS}"
    os.environ.update(SESSION_SET)
    os.environ[SESSION_ROOT_ENV] = str(root)
    os.environ[_SESSION_OWNER_ENV] = str(os.getpid())
    return root


SESSION_ROOT: Final = _enter_session_sandbox()

# Only now import the application: nothing may resolve a path before the
# sandbox is the baseline.
from wall_in_one.theme import noctalia  # noqa: E402

#: The account's real home from the password database, not ``$HOME``.
REAL_HOME: Final = Path(pwd.getpwuid(os.getuid()).pw_dir)
#: What the guard and the tripwire watch below :data:`REAL_HOME`.
REAL_PROFILE_DIRECTORIES: Final = (
    ".config/wall-in-one",
    ".local/state/wall-in-one",
    ".cache/wall-in-one",
    ".local/state/noctalia",
)
#: Noctalia's state directory is the live desktop's: its clipboard, launcher
#: counts, notifications and catalogues change all the time, and it rewrites
#: ``settings.toml`` whenever the wallpaper changes. There the tripwire
#: watches only the names Wall-in-One itself creates beside that file (its
#: settings backups and transaction records), top level only.
NOCTALIA_STATE: Final = ".local/state/noctalia"
NOCTALIA_WATCHED: Final = ("*wall-in-one*", ".settings.toml.backup-*")
#: Rewritten by Noctalia's template renderer when the wallpaper changes.
DESKTOP_WRITTEN: Final = (".local/state/wall-in-one/palette.json",)

_Identity = tuple[int, int, int]


def _desktop_written(relative: str) -> bool:
    """Whether the tripwire skips ``relative`` as the live desktop's own write.

    :func:`_guard_real_profile` still refuses any write to it from this
    process; only the after-the-fact comparison cannot tell whose it was.
    """
    if relative in DESKTOP_WRITTEN:
        return True
    parts = Path(relative).parts
    noctalia = Path(NOCTALIA_STATE).parts
    if parts[: len(noctalia)] != noctalia or len(parts) == len(noctalia):
        return False
    return len(parts) > len(noctalia) + 1 or not any(
        fnmatch.fnmatchcase(parts[len(noctalia)], pattern) for pattern in NOCTALIA_WATCHED
    )


def real_profile_snapshot(home: Path = REAL_HOME) -> dict[str, _Identity | None]:
    """Identity, size and mtime of every entry in the watched directories.

    Reads metadata only and never follows a symbolic link. A watched
    directory that does not exist is recorded as ``None``, so its creation
    counts as a change too. A directory is recorded by identity alone: an
    entry appearing or going is itself a change, and the desktop's own
    temporaries move a directory's mtime all the time.
    """
    found: dict[str, _Identity | None] = {}
    for relative in REAL_PROFILE_DIRECTORIES:
        top = home / relative
        try:
            status = top.lstat()
        except FileNotFoundError:
            found[str(top)] = None
            continue
        except OSError:
            continue
        found[str(top)] = (status.st_ino, 0, 0)
        for directory, directories, files in os.walk(top):
            # Do not even descend into what is skipped (Noctalia's caches).
            directories[:] = [
                name
                for name in directories
                if not _desktop_written(os.path.relpath(os.path.join(directory, name), home))
            ]
            for name in (*directories, *files):
                path = os.path.join(directory, name)
                if _desktop_written(os.path.relpath(path, home)):
                    continue
                try:
                    entry = os.lstat(path)
                except OSError:
                    continue  # vanished mid-walk
                if stat.S_ISDIR(entry.st_mode):
                    found[path] = (entry.st_ino, 0, 0)
                else:
                    found[path] = (entry.st_ino, entry.st_size, entry.st_mtime_ns)
    return found


#: Roots the in-process guard refuses to write below.
GUARDED_ROOTS: list[str] = [str(REAL_HOME / relative) for relative in REAL_PROFILE_DIRECTORIES]
#: Writes the guard refused, for the tripwire to report even when the code
#: under test caught the error and carried on.
GUARD_REFUSALS: list[str] = []
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
#: Audit event -> argument positions of (path, dir_fd) pairs it writes.
_WRITING_EVENTS: Final[dict[str, tuple[tuple[int, int | None], ...]]] = {
    "os.rename": ((0, 2), (1, 3)),  # os.rename and os.replace
    "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),),
    "os.mkdir": ((0, 2),),
    "os.link": ((1, 3),),
    "os.symlink": ((1, 2),),
    "os.truncate": ((0, None),),
    "os.utime": ((0, 3),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "shutil.rmtree": ((0, 1),),
    "shutil.move": ((0, None), (1, None)),
    "shutil.copyfile": ((1, None),),
    "shutil.copytree": ((1, None),),
}
_guard_state = threading.local()


def _resolved(path: object, dir_fd: object) -> str | None:
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str):
        return None
    if not os.path.isabs(path) and isinstance(dir_fd, int) and dir_fd >= 0:
        try:
            path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
        except OSError:
            return None
    return os.path.abspath(path)


def _guarded(path: str) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in GUARDED_ROOTS)


def _guard_real_profile(event: str, arguments: tuple[Any, ...]) -> None:
    """Refuse, from inside this process, any write below the real profile.

    An audit hook sees ``open`` and the path-changing ``os`` and ``shutil``
    calls before they happen, so the write never starts. It cannot see a
    child process, a raw ``ctypes`` call (``file_io``'s no-replace rename),
    or an ``os.open`` relative to a directory descriptor, whose audit event
    leaves the descriptor out; the tripwire covers those.
    """
    if event == "open":
        if len(arguments) < 3:
            return
        mode, flags = arguments[1], arguments[2]
        writing = (isinstance(mode, str) and any(letter in mode for letter in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        if not writing:
            return
        targets: tuple[tuple[int, int | None], ...] = ((0, None),)
    else:
        found = _WRITING_EVENTS.get(event)
        if found is None:
            return
        targets = found
    if getattr(_guard_state, "busy", False):
        return
    _guard_state.busy = True
    try:
        for path_at, fd_at in targets:
            if path_at >= len(arguments):
                continue
            dir_fd = arguments[fd_at] if fd_at is not None and fd_at < len(arguments) else None
            path = _resolved(arguments[path_at], dir_fd)
            if path is not None and _guarded(path):
                refusal = f"{event} {path}"
                GUARD_REFUSALS.append(refusal)
                raise PermissionError(f"a test tried to write the real profile: {refusal}")
    finally:
        _guard_state.busy = False


sys.addaudithook(_guard_real_profile)


def real_profile_changes(
    before: dict[str, _Identity | None], after: dict[str, _Identity | None]
) -> list[str]:
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


#: The real profile as the session found it; advanced after each report so a
#: change is reported once, by the test after which it was first seen.
_real_profile_baseline: dict[str, _Identity | None] = real_profile_snapshot()


def _escapes() -> list[str]:
    """Refused writes and changed paths since the last report; resets both."""
    global _real_profile_baseline
    refused = [f"refused: {refusal}" for refusal in GUARD_REFUSALS]
    GUARD_REFUSALS.clear()
    current = real_profile_snapshot()
    changed = real_profile_changes(_real_profile_baseline, current)
    _real_profile_baseline = current
    return refused + changed


@pytest.fixture(autouse=True)
def real_profile_tripwire() -> Iterator[None]:
    """Fail the test after which the real profile was written or changed.

    Autouse, so it is torn down after every fixture the test requested.
    Session and module fixtures are checked by :func:`pytest_sessionfinish`.
    A change that something else on the desktop made during the test (the
    wallpaper service, a running Wall-in-One) trips it too: the report lists
    the paths so a person can tell which.
    """
    yield
    escapes = _escapes()
    if escapes:
        shown = "\n  ".join(escapes[:20])
        pytest.fail(
            f"the real profile under {REAL_HOME} was written or changed during this test "
            f"({len(escapes)}):\n  {shown}",
            pytrace=False,
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The same check once more, after session and module fixtures are gone."""
    escapes = _escapes()
    if escapes:
        print(
            f"\nthe real profile under {REAL_HOME} was written or changed after the last "
            "test:\n  " + "\n  ".join(escapes[:20])
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


#: Everything in `theme.noctalia` that changes something outside this process.
#: Readers are left alone: they are harmless, several tests rely on them
#: failing naturally when the shell is absent, and pretending they are
#: dangerous would mean patching them everywhere for nothing.
MUTATORS: tuple[str, ...] = (
    "set_wallpaper",
    "set_scheme",
    "set_mode",
    "reload_config",
    "apply_templates",
)

MUTATING_MESSAGES: frozenset[str] = frozenset(
    {
        "wallpaper-set",
        "color-scheme-set",
        "theme-mode-set",
        "config-reload",
        "templates-apply",
    }
)


@pytest.fixture(autouse=True)
def no_live_noctalia(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Refuse desktop mutation and isolate every conventional state path.

    The mutator guard protects Noctalia, but the app also owns playlists,
    schedules, caches and downloads. A test that constructs a default Store
    must not silently resolve those into the developer's home directory. The
    Nix sandbox exposed this when ``HOME=/homeless-shelter`` made such a write
    fail; outside the sandbox it had been succeeding against real app state.

    A test not marked ``gui`` runs as the package's own check does: no
    display and no session bus at all. Under Xvfb that also keeps GLib from
    autolaunching a private bus, and code that asks whether a user-service
    manager exists gets the same answer as in the build sandbox.
    """
    if request.node.get_closest_marker("gui") is None:
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse_internet(connection: socket.socket, address: object) -> None:
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"a test attempted a live network connection to {address!r}")
        real_connect(connection, address)  # type: ignore[arg-type]

    def refuse_internet_ex(connection: socket.socket, address: object) -> int:
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"a test attempted a live network connection to {address!r}")
        return real_connect_ex(connection, address)  # type: ignore[arg-type]

    def refuse_create_connection(*arguments: object, **_keywords: object) -> None:
        target = arguments[0] if arguments else "an internet address"
        raise AssertionError(f"a test attempted a live network connection to {target!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse_internet)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_internet_ex)
    monkeypatch.setattr(socket, "create_connection", refuse_create_connection)

    def refuse(name: str) -> Any:
        def called(*_arguments: object, **_keywords: object) -> None:
            raise AssertionError(
                f"a test called noctalia.{name}, which changes the live desktop. "
                "Patch it in the test if that is what you meant to exercise."
            )

        return called

    for name in MUTATORS:
        monkeypatch.setattr(noctalia, name, refuse(name))

    real_message = noctalia.message

    def guarded_message(
        command: str,
        *arguments: str,
        cancelled: noctalia.CancelCheck | None = None,
    ) -> str:
        if command in MUTATING_MESSAGES:
            raise AssertionError(
                f"a test called noctalia.message({command!r}), which changes the live desktop. "
                "Patch it in the test if that is what you meant to exercise."
            )
        return real_message(command, *arguments, cancelled=cancelled)

    monkeypatch.setattr(noctalia, "message", guarded_message)
    yield
