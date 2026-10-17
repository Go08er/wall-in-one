"""How the test instructions say to run the suite, and the wrapper they name.

tests/conftest.py's guard is a backstop: an audit hook sees only its own
Python process, and the tripwire notices a change after it happened. So no
instruction may present a bare ``pytest`` from a developer shell as safe, and
``tools/isolated.sh``, which they name instead, must really isolate.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests import conftest

REPOSITORY = Path(__file__).resolve().parents[1]
WRAPPER = REPOSITORY / "tools" / "isolated.sh"


@pytest.fixture
def temporary() -> Iterator[Path]:
    """A TMPDIR short enough for the compositor's socket below the wrapper's root.

    A Unix socket's path holds 107 bytes and pytest's own directories are long.
    """
    path = Path(tempfile.mkdtemp(prefix="wio-t", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _development_commands() -> list[str]:
    readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Development", 1)[1].split("\n## ", 1)[0]
    return [
        line.removeprefix("$ ").strip()
        for block in re.findall(r"```console\n(.*?)```", section, re.DOTALL)
        for line in block.splitlines()
        if line.startswith("$ ")
    ]


def test_the_readme_runs_every_test_through_the_wrapper() -> None:
    commands = [command for command in _development_commands() if "pytest" in command]
    assert commands, "the Development section shows how to run the tests"
    for command in commands:
        assert command.startswith("tools/isolated.sh "), command
    readme = " ".join((REPOSITORY / "README.md").read_text(encoding="utf-8").split())
    assert "never as a bare `pytest`" in readme
    assert "That guard is a backstop, not a substitute" in readme


def test_the_guard_says_it_is_a_backstop() -> None:
    assert conftest.__doc__ is not None
    doc = " ".join(conftest.__doc__.split())
    assert "All of this is a backstop, not the way to run the suite." in doc
    assert "``tools/isolated.sh``" in doc and "never as a bare ``pytest``" in doc


#: A stand-in Weston: listens where it is told, records how it was started
#: (its arguments, environment and configuration) in $STUB_WESTON_REPORT,
#: and runs until TERM, as the real one does.
WESTON: Final = """
import json, os, signal, socket, sys
signal.signal(signal.SIGTERM, lambda *_arguments: sys.exit(0))
options = dict(argument[2:].split("=", 1) for argument in sys.argv[1:] if "=" in argument)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(os.path.join(os.environ["XDG_RUNTIME_DIR"], options["socket"]))
listener.listen(1)
report = os.environ.get("STUB_WESTON_REPORT")
if report:
    with open("/proc/self/stat", encoding="utf-8") as stat:
        started = stat.read().rsplit(")", 1)[1].split()[19]
    with open(options["config"], encoding="utf-8") as config:
        seen = {"pid": os.getpid(), "started": started, "argv": sys.argv[1:],
                "environ": dict(os.environ), "config": config.read()}
    with open(report + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(seen, handle)
    os.replace(report + ".tmp", report)
while True:
    signal.pause()
"""


def _stub_weston(stubs: Path) -> None:
    stubs.mkdir()
    weston = stubs / "weston"
    weston.write_text(f"#!{sys.executable}\n{WESTON}", encoding="utf-8")
    weston.chmod(0o755)


def test_the_wrapper_runs_the_command_in_a_throwaway_profile(tmp_path: Path, temporary: Path) -> None:
    """Started from a stand-in desktop shell, inside a Nix shell, with a stand-in
    Weston: the command sees none of that shell, and draws only on that Weston,
    which saw none of it either."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported build has bash
        pytest.skip("no bash")
    stubs = tmp_path / "bin"
    _stub_weston(stubs)
    weston_report = tmp_path / "weston.json"
    shell = tmp_path / "desktop"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(
        HOME=str(shell / "home"),
        XDG_CONFIG_HOME=str(shell / "home" / ".config"),
        XDG_STATE_HOME=str(shell / "home" / ".local" / "state"),
        XDG_CACHE_HOME=str(shell / "home" / ".cache"),
        XDG_DATA_HOME=str(shell / "home" / ".local" / "share"),
        XDG_RUNTIME_DIR=str(shell / "run"),
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={shell / 'run' / 'bus'}",
        DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={shell / 'system_bus_socket'}",
        WAYLAND_DISPLAY="wayland-1",
        WAYLAND_SOCKET="9",
        NIRI_SOCKET=str(shell / "run" / "niri.sock"),
        DISPLAY=":0",
        LD_LIBRARY_PATH=str(shell / "lib"),
        IN_NIX_SHELL="impure",
        TMPDIR=str(temporary),
        PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        STUB_WESTON_REPORT=str(weston_report),
    )
    environment.pop("WIO_TEST_LIVE_RUNTIME_DIRS", None)

    completed = subprocess.run(
        (
            bash,
            str(WRAPPER),
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ)))",
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout.splitlines()[-1])
    root = Path(seen["HOME"]).parent
    assert root.parent == temporary and root.name.startswith("wio-isolated."), root
    layout = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
        "XDG_RUNTIME_DIR": "run",
    }
    for variable, directory in layout.items():
        assert seen[variable] == str(root / directory), variable
    assert seen["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={root / 'run' / 'no-session-bus'}"
    assert seen["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={root / 'run' / 'no-system-bus'}"
    for variable in ("WAYLAND_SOCKET", "NIRI_SOCKET", "DISPLAY", "LD_LIBRARY_PATH"):
        assert variable not in seen, variable
    assert conftest.SESSION_ROOT_ENV not in seen
    for variable, value in {
        "GDK_DEBUG": "no-portals",
        "GTK_USE_PORTAL": "0",
        "GSK_RENDERER": "cairo",
        "GDK_BACKEND": "wayland",
    }.items():
        assert seen[variable] == value, variable
    # The run's own compositor, by absolute path, in a private directory of its
    # own: not the shell's wayland-1, and nowhere in either runtime directory.
    socket_path = Path(seen["WAYLAND_DISPLAY"])
    assert seen[conftest.TEST_WAYLAND_ENV] == str(socket_path)
    assert socket_path.is_absolute() and socket_path.name == "wayland-0", socket_path
    compositor = socket_path.parent
    assert compositor.parent == root and compositor.name.startswith("wio-wl."), compositor
    assert compositor != root / "run", "never below the replaced runtime directory"
    assert seen["WIO_TEST_LIVE_RUNTIME_DIRS"].split(os.pathsep)[0] == str(shell / "run")
    # Weston: headless, in that directory, and handed none of the shell's addresses.
    weston = json.loads(weston_report.read_text(encoding="utf-8"))
    assert weston["environ"]["XDG_RUNTIME_DIR"] == str(compositor)
    for variable in ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "NIRI_SOCKET", "DISPLAY"):
        assert variable not in weston["environ"], variable
    for argument in (
        "--backend=headless",
        "--renderer=pixman",
        "--width=1600",
        "--height=1200",
        "--shell=desktop",
        "--idle-time=0",
        "--socket=wayland-0",
    ):
        assert argument in weston["argv"], argument
    for line in ("idle-time=0", "xwayland=false", "locking=false", "panel-position=none"):
        assert line in weston["config"].splitlines(), line
    assert not _still_running(weston["pid"], weston["started"]), "the compositor is stopped"
    assert not compositor.exists(), "and its directory removed"
    assert not root.exists(), "the throwaway profile is removed afterwards"
    assert not shell.exists(), "nothing was written in the stand-in shell's directories"


#: Reports where it runs, then sleeps far longer than any test waits.
SLEEPER: Final = """
import json, os, sys, time
report = {"pid": os.getpid(), "pgid": os.getpgid(0), "home": os.environ["HOME"]}
with open(sys.argv[1] + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(report, handle)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(300)
"""


def _session(session: int) -> dict[int, tuple[str, str]]:
    """Every live process in ``session``: pid -> (start time, command name)."""
    found: dict[int, tuple[str, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            text = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        name = text[text.index("(") + 1 : text.rindex(")")]
        fields = text[text.rindex(")") + 2 :].split()
        # state, ppid, pgrp, session, ..., starttime (fields 3, 4, 5, 6, 22)
        if fields[0] != "Z" and int(fields[3]) == session:
            found[int(entry)] = (fields[19], name)
    return found


def _descendants(roots: set[int]) -> dict[int, tuple[str, str]]:
    """Every live process below ``roots`` by parent: pid -> (start time, command name).

    Weston starts its shell's client in a session of its own, which
    :func:`_session` does not see.
    """
    table: dict[int, tuple[int, str, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            text = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        fields = text[text.rindex(")") + 2 :].split()
        if fields[0] != "Z":
            table[int(entry)] = (
                int(fields[1]),
                fields[19],
                text[text.index("(") + 1 : text.rindex(")")],
            )
    found: dict[int, tuple[str, str]] = {}
    frontier = set(roots)
    while frontier:
        children = {pid for pid, (parent, _started, _name) in table.items() if parent in frontier}
        children -= found.keys() | roots
        for pid in children:
            found[pid] = table[pid][1:]
        frontier = children
    return found


def _still_running(pid: int, started: str) -> bool:
    """Whether that exact process (same start time) is alive, not a zombie."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    fields = text[text.rindex(")") + 2 :].split()
    return fields[19] == started and fields[0] != "Z"


@pytest.mark.gui
@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_to_the_wrapper_stops_its_whole_workload_before_the_profile_goes(
    tmp_path: Path, temporary: Path, number: signal.Signals
) -> None:
    """Sweep 4 T-3, Codex's case: a real compositor and a sleeping command,
    and the signal sent only to the wrapper's own PID, as a task runner does.
    Before, the profile was removed and the display server and the command
    lived on. Every process this test started is in its own session or, for
    the compositor's shell client, below one of them; cleanup kills only those
    exact processes. No X server is among them, Xwayland included."""
    bash, weston = shutil.which("bash"), shutil.which("weston")
    if bash is None or weston is None:  # pragma: no cover - the gui-tests check has both
        pytest.skip("needs bash and weston")
    report = tmp_path / "report.json"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(IN_NIX_SHELL="impure", TMPDIR=str(temporary))
    wrapper = subprocess.Popen(
        (bash, str(WRAPPER), sys.executable, "-c", SLEEPER, str(report)),
        env=environment,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    owned: dict[int, tuple[str, str]] = {}
    try:
        deadline = time.monotonic() + 60
        while not report.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.exists(), "the command never started"
        seen = json.loads(report.read_text(encoding="utf-8"))
        root = Path(seen["home"]).parent
        assert root.parent == temporary and root.is_dir()
        # The compositor's shell client may still be starting: wait for it.
        names: list[str] = []
        while time.monotonic() < deadline:
            owned = _session(wrapper.pid)
            owned.update(_descendants(set(owned)))
            names = sorted(name for _started, name in owned.values())
            if "weston-desktop-" in names:
                break
            time.sleep(0.05)
        assert seen["pid"] in owned, names
        assert "weston" in names and "weston-desktop-" in names, names
        assert not [name for name in names if name.startswith("X")], names
        assert [path.name[:7] for path in root.iterdir()].count("wio-wl.") == 1

        os.kill(wrapper.pid, number)
        status = wrapper.wait(timeout=30)

        survivors = {
            pid: name for pid, (started, name) in owned.items() if _still_running(pid, started)
        }
        assert survivors == {}, f"left running: {survivors}"
        assert status == 128 + number
        assert wrapper.stderr is not None
        assert "exited during the run" not in wrapper.stderr.read().decode(errors="replace"), (
            "Weston gets the group's signal too; that is not a crash"
        )
        assert not root.exists(), "the profile is removed, after the workload"
        assert list(temporary.iterdir()) == [], "and the compositor's directory"
    finally:
        for pid, (started, _name) in owned.items():
            if pid != wrapper.pid and _still_running(pid, started):
                os.kill(pid, signal.SIGKILL)
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.wait(timeout=30)
        if wrapper.stderr is not None:
            wrapper.stderr.close()


#: Like SLEEPER, but ignores TERM, so only the wrapper's KILL stops it.
DEAF: Final = SLEEPER.replace("import json, os, sys, time", "import json, os, signal, sys, time").replace(
    "report = {", "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nreport = {", 1
)


@pytest.mark.gui
def test_a_kill_after_the_grace_period_leaves_no_compositor_directory(tmp_path: Path, temporary: Path) -> None:
    """A KILL cannot be caught, so the helper cannot remove its own directory.

    The command ignores TERM, so the wrapper's TERM waits out the grace period
    and then KILLs the whole group, the helper included. Before, the helper's
    ``wio-wl.*`` directory (socket, lock, ini, logs) stayed in TMPDIR; now it
    is inside the throwaway root, which the wrapper removes.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported build has bash
        pytest.skip("no bash")
    stubs = tmp_path / "bin"
    _stub_weston(stubs)
    report = tmp_path / "report.json"
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(
        IN_NIX_SHELL="impure",
        TMPDIR=str(temporary),
        PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        WIO_ISOLATED_GRACE_TENTHS="5",
    )
    wrapper = subprocess.Popen(
        (bash, str(WRAPPER), sys.executable, "-c", DEAF, str(report)),
        env=environment,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned: dict[int, tuple[str, str]] = {}
    try:
        deadline = time.monotonic() + 60
        while not report.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.exists(), "the command never started"
        root = Path(json.loads(report.read_text(encoding="utf-8"))["home"]).parent
        assert [path.name[:7] for path in root.iterdir()].count("wio-wl.") == 1
        owned = _session(wrapper.pid)
        os.kill(wrapper.pid, signal.SIGTERM)
        assert wrapper.wait(timeout=30) == 128 + signal.SIGTERM
        survivors = {
            pid: name for pid, (started, name) in owned.items() if _still_running(pid, started)
        }
        assert survivors == {}, f"left running: {survivors}"
        assert list(temporary.iterdir()) == [], "no profile and no compositor directory"
    finally:
        for pid, (started, _name) in owned.items():
            if pid != wrapper.pid and _still_running(pid, started):
                os.kill(pid, signal.SIGKILL)
        if wrapper.poll() is None:
            wrapper.kill()
        wrapper.wait(timeout=30)


# -- the workload's process group: known by more than its number (sweep 5 U-2) ----------


#: Leaves a member in the command's process group, reports its pid and start
#: time, and exits with status 3 at once. The member ignores TERM when asked.
LEAVER: Final = """
import json, os, signal, subprocess, sys
command = [sys.executable, "-c", "import signal, sys, time\\n"
           "if sys.argv[1] == 'stubborn':\\n"
           "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
           "time.sleep(float(sys.argv[2]))\\n", sys.argv[2], sys.argv[3]]
# No shared pipes: the wrapper's caller must not wait on the member's output.
quiet = subprocess.DEVNULL
member = subprocess.Popen(command, stdin=quiet, stdout=quiet, stderr=quiet)
with open(f"/proc/{member.pid}/stat", encoding="utf-8") as handle:
    fields = handle.read().rsplit(")", 1)[1].split()
report = {"pid": member.pid, "started": fields[19], "pgrp": fields[2], "home": os.environ["HOME"]}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
os._exit(3)
"""


def _stubbed_wrapper_environment(tmp_path: Path, temporary: Path, grace_tenths: int) -> dict[str, str]:
    """The wrapper inside a Nix shell, with a stand-in Weston."""
    stubs = tmp_path / "bin"
    _stub_weston(stubs)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in (conftest.SESSION_ROOT_ENV, "WIO_TEST_SESSION_PID")
    }
    environment.update(
        IN_NIX_SHELL="impure",
        TMPDIR=str(temporary),
        PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        WIO_ISOLATED_GRACE_TENTHS=str(grace_tenths),
    )
    return environment


@pytest.mark.parametrize(
    ("kind", "seconds", "grace_tenths"),
    [("polite", "1.0", 50), ("stubborn", "300", 5), ("polite", "300", 5)],
)
def test_the_profile_goes_only_after_the_whole_group_has(
    tmp_path: Path, temporary: Path, kind: str, seconds: str, grace_tenths: int
) -> None:
    """A member the command leaves behind is waited for, then stopped, before the profile goes.

    Before, the wrapper knew its group only by number and returned when the
    command did: the member went on running (in a group whose number could
    later be reused) with its profile deleted under it.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - every supported build has bash
        pytest.skip("no bash")
    report = tmp_path / "report.json"
    started = time.monotonic()
    completed = subprocess.run(
        (bash, str(WRAPPER), sys.executable, "-c", LEAVER, str(report), kind, seconds),
        env=_stubbed_wrapper_environment(tmp_path, temporary, grace_tenths),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        start_new_session=True,
    )
    elapsed = time.monotonic() - started
    seen = json.loads(report.read_text(encoding="utf-8"))
    try:
        assert completed.returncode == 3, completed.stderr
        assert not _still_running(seen["pid"], seen["started"]), "the member outlived the wrapper"
        assert not Path(seen["home"]).parent.exists(), "the profile is removed, after the group"
        if seconds == "1.0":
            assert elapsed >= 0.9, "a member that leaves by itself is waited for"
        else:
            # Grace, TERM, grace, then KILL for the one that ignores TERM.
            assert elapsed < 30, elapsed
    finally:
        if _still_running(seen["pid"], seen["started"]):
            os.kill(seen["pid"], signal.SIGKILL)


def test_the_wrapper_never_signals_a_group_by_its_number_alone() -> None:
    """Every group signal is guarded by the leader's identity: its parent and start time."""
    script = "\n".join(
        line
        for line in WRAPPER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "kill -0" not in script, "existence of a number is not ownership"
    assert "${!" not in script, "no fallback to the last background job's number"
    body = script.split("stop_workload() {", 1)[1].split("\n}\n", 1)[0]
    guarded = [line for line in body.splitlines() if "kill -s" in line]
    assert len(guarded) == 2
    lines = body.splitlines()
    for line in guarded:
        before = lines[: lines.index(line)]
        assert any("leader_alive" in earlier for earlier in before[-3:]), line
    alive = script.split("leader_alive() {", 1)[1].split("\n}\n", 1)[0]
    assert '"$$"' in alive and "leader_started" in alive and "!= Z" in alive
