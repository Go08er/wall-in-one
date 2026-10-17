"""A module-scoped ``Gtk.init()`` already runs in the session sandbox.

GUI test modules initialize GTK in a module-scoped fixture, which runs before
conftest's function-scoped isolation. GLib also caches the user directories
the first time anything asks for them. So the sandbox has to be the process
baseline from conftest's import on, buses and portals included; this module
initializes GTK the same way and checks what it saw.

The display is part of that baseline: GTK draws on the run's own headless
Wayland compositor and on nothing else (conftest's display rules). Here that
is checked on GTK's own connection, by the address of the socket it is
connected to, and in child processes started with a stand-in desktop's
``WAYLAND_DISPLAY``, whose stand-in compositor counts every connection.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from tests import conftest  # noqa: E402

_SEEN: dict[str, str] = {}
REPOSITORY: Final = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    """As every GUI module does it, and recording the environment it ran in."""
    _SEEN.update(os.environ)
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - conftest fails a GUI test with no compositor first
        pytest.skip("no display")
    _SEEN.update(
        {
            "glib-home": GLib.get_home_dir(),
            "glib-config": GLib.get_user_config_dir(),
            "glib-state": GLib.get_user_state_dir(),
            "glib-cache": GLib.get_user_cache_dir(),
            "glib-data": GLib.get_user_data_dir(),
            "glib-runtime": GLib.get_user_runtime_dir(),
        }
    )


def test_gtk_was_initialized_inside_the_session_sandbox() -> None:
    root = conftest.SESSION_ROOT
    for variable, name in conftest.SESSION_LAYOUT.items():
        assert _SEEN[variable] == str(root / name), variable
    runtime = root / conftest.SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    assert _SEEN["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / conftest.DEAD_SESSION_BUS}"
    assert _SEEN["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={runtime / conftest.DEAD_SYSTEM_BUS}"
    for variable, value in conftest.SESSION_SET.items():
        assert _SEEN[variable] == value, variable
    for variable in conftest.SESSION_UNSET:
        assert variable not in _SEEN, variable
    assert "DISPLAY" not in _SEEN, "no X11 display, ever"
    assert _SEEN["GDK_BACKEND"] == "wayland", "no fallback to another backend"
    assert conftest.TEST_WAYLAND_SOCKET is not None, conftest.TEST_WAYLAND_REFUSAL
    assert _SEEN["WAYLAND_DISPLAY"] == conftest.TEST_WAYLAND_SOCKET
    assert os.path.isabs(_SEEN["WAYLAND_DISPLAY"]), "it must not depend on XDG_RUNTIME_DIR"


def test_glib_never_cached_the_real_home_directories() -> None:
    """GLib keeps its first answer for the whole process.

    Which sandbox directory that is depends on what asked first (the session
    root, or an earlier test's own); it is never the account's real ones.
    """
    real = conftest.REAL_HOME
    defaults = {
        real,
        real / ".config",
        real / ".local" / "state",
        real / ".cache",
        real / ".local" / "share",
    }
    for key in ("home", "config", "state", "cache", "data", "runtime"):
        cached = Path(_SEEN[f"glib-{key}"])
        assert cached not in defaults, (key, cached)
        for guarded in conftest.GUARDED_ROOTS:
            assert not cached.is_relative_to(guarded), (key, cached)


def _peers() -> list[str]:
    """Where this process's Unix stream sockets are connected to.

    For a connected Unix socket, the peer's name is the path its server is
    listening on: for GTK's Wayland connection, the compositor's socket.
    Reads this process's own descriptors; connects to nothing.
    """
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            if not stat.S_ISSOCK(os.fstat(int(name)).st_mode):
                continue
            duplicate = socket.socket(fileno=os.dup(int(name)))
        except OSError:
            continue  # the listing's own descriptor, or one closed meanwhile
        with duplicate:
            if duplicate.family != socket.AF_UNIX:
                continue
            try:
                peer = duplicate.getpeername()
            except OSError:
                continue
            if isinstance(peer, str) and peer:
                found.append(peer)
    return found


def test_gtk_draws_on_the_runs_own_compositor_and_nothing_else() -> None:
    display = Gdk.Display.get_default()
    assert display is not None
    # GdkWayland's class, by its GType's name unless that typelib is loaded.
    assert type(display).__name__ in {"GdkWaylandDisplay", "WaylandDisplay"}, type(display)
    peers = _peers()
    assert conftest.TEST_WAYLAND_SOCKET in peers, peers
    live = [
        Path(os.path.realpath(directory))
        for directory in conftest.live_runtime_dirs(os.environ, os.getuid())
    ]
    for peer in peers:
        for directory in live:
            assert not Path(os.path.realpath(peer)).is_relative_to(directory), (peer, directory)


# -- a child process started from a stand-in desktop ----------------------------------


class StandInCompositor:
    """A listening socket where a desktop's compositor would be, counting connections.

    Every connection is accepted and closed at once, so a client that did
    connect fails quickly instead of waiting for an answer.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connections = 0
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(8)
        self._listener.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()


#: Imports conftest as pytest does first, then GTK, and reports what GTK connected to.
CHILD: Final = """
import json, os, socket, stat, sys
sys.path.insert(0, sys.argv[1])
from tests import conftest
from tests.test_ui_sandbox_isolation import _peers
import gi
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
# Importing Gtk initializes it (PyGObject), as in a test module.
from gi.repository import Gdk, Gtk
display = Gdk.Display.get_default()
print(json.dumps({
    "accepted": conftest.TEST_WAYLAND_SOCKET,
    "refusal": conftest.TEST_WAYLAND_REFUSAL,
    "display": display.__gtype__.name if display is not None else None,
    "peers": _peers(),
    "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY"),
    "DISPLAY": os.environ.get("DISPLAY"),
}))
"""


@pytest.fixture
def stand_in_desktop(tmp_path: Path) -> Iterator[tuple[dict[str, str], StandInCompositor]]:
    """A child's environment as a desktop terminal exports it, and that desktop's compositor."""
    shell = tmp_path / "desk"
    run = shell / "run"
    run.mkdir(parents=True, mode=0o700)
    compositor = StandInCompositor(run / "wayland-1")
    environment = {
        name: value
        for name, value in os.environ.items()
        if name
        not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID", "WIO_TEST_LIVE_RUNTIME_DIRS")
    }
    environment.update(
        XDG_RUNTIME_DIR=str(run),
        WAYLAND_DISPLAY="wayland-1",
        NIRI_SOCKET=str(run / "niri.sock"),
        PYTHONPATH=os.pathsep.join((str(REPOSITORY), str(REPOSITORY / "src"))),
    )
    try:
        yield environment, compositor
    finally:
        compositor.close()


def _child(environment: dict[str, str]) -> dict[str, object]:
    completed = subprocess.run(
        (sys.executable, "-c", CHILD, str(REPOSITORY)),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report: dict[str, object] = json.loads(completed.stdout.splitlines()[-1])
    return report


def test_an_inherited_live_wayland_display_is_not_what_gtk_uses(
    stand_in_desktop: tuple[dict[str, str], StandInCompositor],
) -> None:
    """The terminal's ``WAYLAND_DISPLAY=wayland-1`` is dropped; GTK takes the run's own."""
    environment, desktop = stand_in_desktop
    assert conftest.TEST_WAYLAND_SOCKET is not None
    environment[conftest.TEST_WAYLAND_ENV] = conftest.TEST_WAYLAND_SOCKET

    report = _child(environment)

    assert report["display"] == "GdkWaylandDisplay"
    assert report["accepted"] == conftest.TEST_WAYLAND_SOCKET
    assert report["WAYLAND_DISPLAY"] == conftest.TEST_WAYLAND_SOCKET
    peers = report["peers"]
    assert isinstance(peers, list)
    assert conftest.TEST_WAYLAND_SOCKET in peers and str(desktop.path) not in peers, peers
    assert desktop.connections == 0, "the desktop's compositor was reached"


@pytest.mark.parametrize("pointed_at", ["the desktop's socket", "unset"])
def test_without_a_compositor_of_its_own_gtk_reaches_no_display(
    stand_in_desktop: tuple[dict[str, str], StandInCompositor], pointed_at: str
) -> None:
    """Refused or missing, there is no display at all: no fallback to the desktop's."""
    environment, desktop = stand_in_desktop
    environment.pop(conftest.TEST_WAYLAND_ENV, None)
    if pointed_at == "the desktop's socket":
        environment[conftest.TEST_WAYLAND_ENV] = str(desktop.path)

    report = _child(environment)

    assert report["accepted"] is None
    assert report["refusal"], report
    assert report["display"] is None, report
    assert report["WAYLAND_DISPLAY"] is None
    assert report["DISPLAY"] is None
    assert desktop.connections == 0, "the desktop's compositor was reached"


#: A GUI test, run by a child pytest that has no compositor of its own.
LONE_GUI_TEST: Final = """
import pytest

@pytest.mark.gui
def test_needs_a_display():
    pass
"""


def test_a_gui_test_without_a_compositor_fails_and_is_never_skipped(
    stand_in_desktop: tuple[dict[str, str], StandInCompositor], tmp_path: Path
) -> None:
    environment, desktop = stand_in_desktop
    environment.pop(conftest.TEST_WAYLAND_ENV, None)
    (tmp_path / "test_lone.py").write_text(LONE_GUI_TEST, encoding="utf-8")

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-rA",
            str(tmp_path / "test_lone.py"),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert completed.returncode != 0, completed.stdout
    assert "1 error" in completed.stdout and "skipped" not in completed.stdout, completed.stdout
    assert "has no headless Wayland compositor of the run's own" in completed.stdout
    assert "WIO_TEST_WAYLAND_SOCKET is not set" in completed.stdout
    assert desktop.connections == 0
