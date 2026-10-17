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
portals, and drops the desktop's display addresses -- all before any test
module imports or initializes GTK, whose module-scoped setup runs before the
per-test guard below. Restoring the environment
can then only ever land there. Below that, an audit hook refuses any write
this process attempts under the real profile directories, and a tripwire
fails the test after which they were written or changed.

The display rules. Wall-in-One supports only Wayland, and so do its tests:
GTK gets ``GDK_BACKEND=wayland`` and no X11 ``DISPLAY`` at all. The only
Wayland display a test may reach is the run's own headless compositor, which
``tools/headless-wayland.sh`` starts and names, by absolute path, in
``WIO_TEST_WAYLAND_SOCKET``. An inherited ``WAYLAND_DISPLAY`` (``wayland-1``
from a developer's terminal), ``WAYLAND_SOCKET`` and ``NIRI_SOCKET`` are
dropped. ``WIO_TEST_WAYLAND_SOCKET`` itself is taken only when it is a socket
of this account's, not a symbolic link, in a directory no one else can write,
and outside the desktop's runtime directories (``/run/user/<uid>`` and the
``XDG_RUNTIME_DIR`` the run started with), when it is not one of the sockets
found there (or the inherited ``WAYLAND_DISPLAY`` and ``NIRI_SOCKET``) under
another name, and when the process listening on it, found through /proc
without connecting, is a headless Weston whose own ``XDG_RUNTIME_DIR`` (read
from /proc/<pid>/environ) is the socket's directory. Then ``WAYLAND_DISPLAY`` is set to
its resolved path, absolute, so it still resolves after ``XDG_RUNTIME_DIR`` is
replaced. A test marked ``gui`` that has
no such compositor fails, naming the reason; it is never skipped. A test not
marked ``gui`` gets no ``WAYLAND_DISPLAY`` at all.

All of this is a backstop, not the way to run the suite. An audit hook sees
only this Python process, not a child process or native code, and the
tripwire notices a change only after it happened. Run the tests through
``tools/isolated.sh`` or the flake's checks (README, "Development"), never as
a bare ``pytest`` from a developer shell.
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
from collections.abc import Callable, Iterator, Mapping
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
#: Addresses of displays other than the run's own compositor: X11's, a
#: Wayland connection handed down by file descriptor, and niri's IPC socket.
#: ``WAYLAND_DISPLAY`` is dropped as well, then set only to the test socket.
SESSION_UNSET: Final = ("DISPLAY", "WAYLAND_SOCKET", "NIRI_SOCKET")
#: The run's own headless compositor, by absolute path (tools/headless-wayland.sh).
TEST_WAYLAND_ENV: Final = "WIO_TEST_WAYLAND_SOCKET"
#: The desktop's runtime directories as the first conftest import in this
#: process tree found them, so that a child process (which starts with this
#: one's sandbox as its ``XDG_RUNTIME_DIR``) still refuses the real ones.
_LIVE_RUNTIME_ENV: Final = "WIO_TEST_LIVE_RUNTIME_DIRS"
_WAYLAND_REFUSAL_ENV: Final = "WIO_TEST_WAYLAND_REFUSAL"
#: The session bus gets a dead address below the session root rather than
#: none: an unset address lets GLib look for a bus of its own (with an X11
#: display, autolaunch a private dbus-daemon and document portals that outlive
#: the run). The system bus gets one too: unset, it means the machine's real one.
DEAD_SESSION_BUS: Final = "no-session-bus"
DEAD_SYSTEM_BUS: Final = "no-system-bus"
#: Portals would need that bus anyway; GTK is told not to look for them. GTK
#: draws on Wayland or not at all: never a fallback to another backend.
SESSION_SET: Final = {"GDK_DEBUG": "no-portals", "GTK_USE_PORTAL": "0", "GDK_BACKEND": "wayland"}


def live_runtime_dirs(environ: Mapping[str, str], uid: int) -> tuple[Path, ...]:
    """The desktop's runtime directories: no test compositor may live below one."""
    found = [Path(f"/run/user/{uid}")]
    inherited = environ.get(_LIVE_RUNTIME_ENV, "")
    found.extend(Path(entry) for entry in inherited.split(os.pathsep) if os.path.isabs(entry))
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if os.path.isabs(runtime):
        found.append(Path(runtime))
    unique: list[Path] = []
    for directory in found:
        if directory not in unique:
            unique.append(directory)
    return tuple(unique)


def _socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        status = os.lstat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino) if stat.S_ISSOCK(status.st_mode) else None


def _live_sockets(
    environ: Mapping[str, str], directories: tuple[Path, ...]
) -> set[tuple[int, int]]:
    """(device, inode) of every socket the desktop is known to listen on.

    Every socket directly in its runtime directories, and the inherited
    ``WAYLAND_DISPLAY`` and ``NIRI_SOCKET``, so that a hard link to one of
    them is still recognized. Reads metadata only; connects to nothing.
    """
    candidates: list[Path] = []
    for directory in directories:
        try:
            with os.scandir(directory) as entries:
                candidates.extend(Path(entry.path) for entry in entries)
        except OSError:
            continue  # not there (the build sandbox has no /run/user)
    wayland = environ.get("WAYLAND_DISPLAY", "")
    if wayland and wayland != environ.get(TEST_WAYLAND_ENV):
        runtime = environ.get("XDG_RUNTIME_DIR", "")
        candidates.append(Path(wayland) if os.path.isabs(wayland) else Path(runtime, wayland))
    niri = environ.get("NIRI_SOCKET", "")
    if niri:
        candidates.append(Path(niri))
    return {identity for path in candidates if (identity := _socket_identity(path)) is not None}


#: ``__SO_ACCEPTCON`` in /proc/net/unix's flags: a socket that listens.
_LISTENING: Final = 0x10000


def _listening_pid(path: str) -> int | None:
    """The process of this account's listening on the Unix socket at ``path``.

    Found through /proc (the kernel's table of Unix sockets, then which of
    this account's processes holds one of them), so nothing connects to it.
    None when no process this account can see is listening there.
    """
    wanted = os.path.realpath(path)
    inodes: set[str] = set()
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as table:
            next(table, None)
            for line in table:
                fields = line.rstrip("\n").split(None, 7)
                if (
                    len(fields) == 8
                    and int(fields[3], 16) & _LISTENING
                    and os.path.realpath(fields[7]) == wanted
                ):
                    inodes.add(f"socket:[{fields[6]}]")
    except OSError, ValueError:
        return None
    if not inodes:
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            descriptors = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue  # another account's, or gone
        for descriptor in descriptors:
            try:
                held = os.readlink(f"/proc/{pid}/fd/{descriptor}")
            except OSError:
                continue
            if held in inodes:
                return int(pid)
    return None


def listening_command(path: str) -> list[str] | None:
    """The command line of the process listening on the Unix socket at ``path``.

    Read from /proc; nothing connects. None when no process of this account's
    listens there, or its command line cannot be read.
    """
    pid = _listening_pid(path)
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as command:
            return [os.fsdecode(part) for part in command.read().split(b"\0") if part]
    except OSError:
        return None


def listening_runtime_dir(path: str) -> str | None:
    """The ``XDG_RUNTIME_DIR`` the process listening at ``path`` was started with.

    Read from that process's /proc/<pid>/environ (its environment at exec, which
    is what tools/headless-wayland.sh sets for Weston); nothing connects.
    None when nobody listens there, or the environment cannot be read.
    """
    pid = _listening_pid(path)
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as environment:
            for entry in environment.read().split(b"\0"):
                if entry.startswith(b"XDG_RUNTIME_DIR="):
                    return os.fsdecode(entry.partition(b"=")[2])
    except OSError:
        return None
    return None


def wayland_test_socket(
    environ: Mapping[str, str],
    uid: int,
    live: tuple[Path, ...],
    serving: Callable[[str], list[str] | None] = listening_command,
    runtime_dir: Callable[[str], str | None] = listening_runtime_dir,
) -> tuple[str | None, str]:
    """The run's own compositor socket from ``environ``, or None and why not.

    ``live`` is :func:`live_runtime_dirs`; ``serving`` finds the command
    listening on a socket (:func:`listening_command`) and ``runtime_dir`` the
    ``XDG_RUNTIME_DIR`` it runs with (:func:`listening_runtime_dir`). See the module's
    display rules.
    """
    candidate = environ.get(TEST_WAYLAND_ENV, "")
    if not candidate:
        return None, f"{TEST_WAYLAND_ENV} is not set"
    path = Path(candidate)
    if not path.is_absolute():
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} is not an absolute path"
    try:
        status = os.lstat(path)
    except OSError as error:
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} is not there ({error.strerror})"
    if not stat.S_ISSOCK(status.st_mode):
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} is not a socket"
    if status.st_uid != uid:
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} belongs to another account"
    resolved = Path(os.path.realpath(path))
    try:
        parent = os.stat(resolved.parent)
    except OSError as error:
        return None, f"{TEST_WAYLAND_ENV}={candidate!r}: its directory is unreadable ({error})"
    if parent.st_uid != uid or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} is in a directory others can write"
    for directory in live:
        if resolved.is_relative_to(Path(os.path.realpath(directory))):
            return None, (
                f"{TEST_WAYLAND_ENV}={candidate!r} is in the desktop's runtime directory "
                f"{directory}, where its own compositor's sockets are"
            )
    if (status.st_dev, status.st_ino) in _live_sockets(environ, live):
        return None, f"{TEST_WAYLAND_ENV}={candidate!r} is one of the desktop's own sockets"
    command = serving(str(path))
    if command is None:
        return None, f"{TEST_WAYLAND_ENV}={candidate!r}: no process of this account's listens on it"
    if (
        not command
        or os.path.basename(command[0]) != "weston"
        or "--backend=headless" not in command
    ):
        return None, (
            f"{TEST_WAYLAND_ENV}={candidate!r} is served by {' '.join(command[:2]) or '?'!r}, "
            "not a headless Weston"
        )
    # tools/headless-wayland.sh starts Weston with XDG_RUNTIME_DIR set to the
    # directory of its socket. Any other headless Weston (another project's, say)
    # is not this run's compositor, whatever its command line looks like.
    runtime = runtime_dir(str(path))
    if runtime is None or os.path.realpath(runtime) != str(resolved.parent):
        return None, (
            f"{TEST_WAYLAND_ENV}={candidate!r} is served by a Weston whose own runtime "
            f"directory ({runtime or 'unreadable'}) is not the socket's directory"
        )
    return str(resolved), ""


def _enter_session_sandbox() -> tuple[Path, str | None, str]:
    """Make a private temporary root the whole process's baseline environment.

    Returns the root and :func:`wayland_test_socket`'s answer.
    """
    existing = os.environ.get(SESSION_ROOT_ENV)
    if existing and os.environ.get(_SESSION_OWNER_ENV) == str(os.getpid()):
        # This module imported twice in one process: the environment already
        # is the sandbox, and the runtime directories are the ones recorded.
        root = Path(existing)
        live = tuple(
            Path(entry)
            for entry in os.environ.get(_LIVE_RUNTIME_ENV, "").split(os.pathsep)
            if entry
        )
    else:
        root = Path(tempfile.mkdtemp(prefix="wio-test-session-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        live = live_runtime_dirs(os.environ, os.getuid())
        os.environ.pop(_WAYLAND_REFUSAL_ENV, None)
    wayland, refusal = wayland_test_socket(os.environ, os.getuid(), live)
    # A refusal is kept for a second import of this module, which no longer
    # sees the inherited addresses it may have depended on.
    refusal = os.environ.get(_WAYLAND_REFUSAL_ENV) or refusal
    if wayland is None:
        os.environ[_WAYLAND_REFUSAL_ENV] = refusal
    os.environ[_LIVE_RUNTIME_ENV] = os.pathsep.join(str(directory) for directory in live)
    os.chmod(root, 0o700)
    for variable, name in SESSION_LAYOUT.items():
        directory = root / name
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        os.environ[variable] = str(directory)
    for variable in (*SESSION_UNSET, "WAYLAND_DISPLAY"):
        os.environ.pop(variable, None)
    if wayland is None:
        os.environ.pop(TEST_WAYLAND_ENV, None)
    else:
        # Both names hold the resolved path, so a second import judges the same
        # socket the first one took (and children see one spelling of it).
        os.environ["WAYLAND_DISPLAY"] = wayland
        os.environ[TEST_WAYLAND_ENV] = wayland
    runtime = root / SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / DEAD_SESSION_BUS}"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={runtime / DEAD_SYSTEM_BUS}"
    os.environ.update(SESSION_SET)
    os.environ[SESSION_ROOT_ENV] = str(root)
    os.environ[_SESSION_OWNER_ENV] = str(os.getpid())
    return root, wayland, refusal


_SANDBOX: Final = _enter_session_sandbox()
SESSION_ROOT: Final = _SANDBOX[0]
#: The run's own compositor socket GUI tests draw on, or None: then every GUI
#: test fails with :data:`TEST_WAYLAND_REFUSAL` (see the module's display rules).
TEST_WAYLAND_SOCKET: Final = _SANDBOX[1]
TEST_WAYLAND_REFUSAL: Final = _SANDBOX[2]

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
    # Noctalia's configuration: its plugins (the companion among them),
    # colour schemes and settings. Unlike its state, nothing rewrites it
    # behind the user's back, so the whole tree is watched.
    ".config/noctalia",
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

#: (inode, size, mtime, st_mode): ``st_mode`` carries the file type and the
#: permission bits, so a chmod (which leaves the mtime alone) is a change too.
_Identity = tuple[int, int, int, int]


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
    """Identity, size, mtime, type and permissions of every watched entry.

    Reads metadata only and never follows a symbolic link. A watched
    directory that does not exist is recorded as ``None``, so its creation
    counts as a change too. A directory is recorded by identity, type and
    permissions, not size or mtime: an entry appearing or going is itself a
    change, and the desktop's own temporaries move a directory's mtime all
    the time. The type and permissions catch what the in-process guard
    cannot see, such as a child process's chmod.
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
        found[str(top)] = (status.st_ino, 0, 0, status.st_mode)
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
                    found[path] = (entry.st_ino, 0, 0, entry.st_mode)
                else:
                    found[path] = (entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_mode)
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


#: What GTK says when a widget that reads Pango markup (a row's title, a toast,
#: a group's description) is given text that isn't markup, "Frog & toad" say:
#: it then shows nothing at all. Text people, files or providers wrote is
#: never markup (`wall_in_one.ui.plain_text`).
MARKUP_FAILURE: Final = "from markup due to error parsing markup"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail, never skip, a GUI test that has no compositor of the run's own.

    First, before any fixture: a GUI module's own setup would otherwise find
    no display and skip, and a skipped GUI test is a gate that passed for
    nothing.
    """
    if item.get_closest_marker("gui") is not None and TEST_WAYLAND_SOCKET is None:
        pytest.fail(
            "this GUI test has no headless Wayland compositor of the run's own "
            f"({TEST_WAYLAND_REFUSAL}). GUI tests draw only there, never on the desktop: "
            "run them through tools/isolated.sh or the gui-tests check "
            '(README, "Development").',
            pytrace=False,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Fail a test during which GTK could not show some text as markup.

    GTK only warns, on standard error, which pytest captures per phase; this
    reads the phase's own capture. A run with ``-s`` captures nothing and so
    checks nothing.
    """
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if not report.passed:
        return
    said = "".join(
        content for name, content in report.sections if name == f"Captured stderr {report.when}"
    )
    failures = [line for line in said.splitlines() if MARKUP_FAILURE in line]
    if failures:
        report.outcome = "failed"
        report.longrepr = (
            f"GTK could not show text as markup during {report.when} "
            f"(it shows nothing instead):\n  " + "\n  ".join(failures[:5])
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
    display and no session bus at all, so code that asks whether a
    user-service manager exists gets the same answer as in the build sandbox.
    A ``gui`` test gets the run's own compositor as ``WAYLAND_DISPLAY`` again,
    whatever an earlier test left there. No test gets an X11 ``DISPLAY``.
    """
    if request.node.get_closest_marker("gui") is None:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv(TEST_WAYLAND_ENV, raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    elif TEST_WAYLAND_SOCKET is not None:
        monkeypatch.setenv("WAYLAND_DISPLAY", TEST_WAYLAND_SOCKET)
        monkeypatch.setenv(TEST_WAYLAND_ENV, TEST_WAYLAND_SOCKET)
    for variable in SESSION_UNSET:
        monkeypatch.delenv(variable, raising=False)

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
