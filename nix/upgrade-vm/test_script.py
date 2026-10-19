# The upgrade scenario, run by the NixOS test driver: the shipped v0.1.4 runs
# on the golden profile, this build is installed over it, starts, idles and
# then uses a per-playlist interval, a rule name and the display opt-in, and
# restarts on that profile. Updates are one-way: nothing here goes back.
#
# upgrade-vm-test.nix prepends the constants it uses (OLD, NEW, HOME,
# RUNTIME_DIR, PROFILE, DRIVER_LOG, NOCTALIA_PROBE, SUPPORT, GUEST_PYTHON, PACKAGE_PYTHON,
# SITE_PACKAGES, BASH, STRICT). `machine`, `start_all` and `subtest`
# are the driver's. Every write to the profile is judged with
# tests/golden/harness.py's own diff and allowance machinery, on snapshots the
# guest takes of the trees Wall-in-One owns or reads.

import importlib
import json
import shlex
import sys
import time
import tomllib
from collections.abc import Callable
from functools import partial
from typing import Any

# The harness and the guest tool live in the support store path, outside the
# driver's environment, so they are loaded at run time, not resolved statically.
sys.path.insert(0, SUPPORT)
harness = importlib.import_module("harness")
vm_tool = importlib.import_module("vm_tool")

q = shlex.quote
STATE_REL = ".local/state/wall-in-one"
STATE = f"{HOME}/{STATE_REL}"
RUNTIME = f"{STATE}/runtime.toml"
RUNTIME_REL = f"{STATE_REL}/runtime.toml"
OVERRIDES = f"{STATE}/runtime-overrides.toml"
OVERRIDES_REL = f"{STATE_REL}/runtime-overrides.toml"
RUNTIME_SOCKET = f"{RUNTIME_DIR}/wall-in-one-runtime.sock"
HEALTH = "wall-in-one-health-sync.service"
#: The fixture's default playlist ("Evenings", all video), which plays at the
#: seeded clock because no timed rule is active then.
EVENINGS = "bd7adc21870f0664"
#: The enabled Evenings rule, 21:00-04:00.
NAMED_RULE = "af5d9a02e281e683"
RULE_NAME = "Evening lights"
#: Assigned in the fixture's displays.json, so the opt-in is meaningful.
OPT_IN = "DP-1"
INTERVAL = 120
GLOBAL_INTERVAL = 900
STORES = ("playlists.json", "schedules.json", "displays.json")

failures: list[str] = []
observations: dict[str, Any] = {}


# -- the guest -------------------------------------------------------------------------


def user_environment() -> str:
    return (
        f"HOME={HOME} USER=wallpaper "
        f"XDG_CONFIG_HOME={HOME}/.config XDG_STATE_HOME={HOME}/.local/state "
        f"XDG_CACHE_HOME={HOME}/.cache XDG_DATA_HOME={HOME}/.local/share "
        f"XDG_RUNTIME_DIR={RUNTIME_DIR} WAYLAND_DISPLAY=wayland-1 "
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={RUNTIME_DIR}/bus "
        f"XDG_DATA_DIRS={PROFILE}/share:/run/current-system/sw/share "
        # The session's PATH (vm-base sets it on the user manager): a compile
        # resolves its renderer programs from PATH, so a different one here
        # would rewrite runtime.toml's program paths for no reason of the app's.
        f"PATH={NOCTALIA_PROBE}/bin:{PROFILE}/bin:/run/current-system/sw/bin LANG=C.UTF-8"
    )


def user(command: str) -> str:
    return (
        f"runuser -u wallpaper -- env -i {user_environment()} "
        f"{BASH} -euo pipefail -c {q(command)}"
    )


def run(command: str) -> str:
    return machine.succeed(user(command)).strip()


def ctl(package: str, arguments: str) -> str:
    return run(f"{package}/bin/wall-in-one ctl {arguments}")


def status(package: str) -> dict[str, Any]:
    value = json.loads(ctl(package, "status"))
    assert isinstance(value, dict) and value["config_path"] == RUNTIME, value
    return value


def wait_status(
    package: str, predicate: Callable[[dict[str, Any]], bool], timeout: float = 45
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        code, output = machine.execute(user(f"{package}/bin/wall-in-one ctl status"))
        last = (code, output)
        if code == 0:
            try:
                value = json.loads(output)
            except ValueError:
                pass
            else:
                last = value
                if isinstance(value, dict) and predicate(value):
                    return value
        time.sleep(0.5)
    raise AssertionError(f"{package} ctl status never matched: {last}")


def prop(name: str, unit: str = "wall-in-one.service") -> str:
    return run(f"systemctl --user show -p {name} --value {unit}")


def check_loaded(package: str) -> None:
    """The user manager holds this package's three units, verbatim."""
    fragment = prop("FragmentPath")
    resolved = machine.succeed(f"readlink -e {q(fragment)}").strip()
    assert resolved == f"{package}/share/systemd/user/wall-in-one.service", (fragment, resolved)
    start = prop("ExecStart")
    assert f"path={package}/bin/wall-in-one-service " in start, start
    prepare = prop("ExecStartPre")
    assert f"{package}/bin/wall-in-one --service-startup-prepare" in prepare, prepare
    assert f"{package}/bin/wall-in-one-service --check-config" in prepare, prepare
    stop = prop("ExecStop")
    assert f"{package}/bin/wall-in-one --sync-runtime-health-on-stop" in stop, stop
    health = prop("ExecStart", HEALTH)
    assert f"{package}/bin/wall-in-one --sync-runtime-health" in health, health


def check_running(package: str) -> str:
    pid = prop("MainPID")
    assert pid.isdigit() and int(pid) > 1, pid
    executable = machine.succeed(f"readlink -e /proc/{pid}/exe").strip()
    assert executable == f"{package}/bin/wall-in-one-service", (pid, executable, package)
    return pid


def install(package: str) -> None:
    """Flip the profile to ``package`` and reload, as a profile upgrade does."""
    run(f"ln -sfn {q(package)} {q(PROFILE)}")
    run("systemctl --user daemon-reload")
    assert run(f"readlink -e {PROFILE}/bin/wall-in-one") == f"{package}/bin/wall-in-one"
    check_loaded(package)


def restart() -> None:
    run("systemctl --user restart wall-in-one.service")
    machine.wait_until_succeeds(user("systemctl --user is-active wall-in-one.service"), timeout=60)
    machine.wait_for_file(RUNTIME_SOCKET)


def wait_health_sync(package: str) -> None:
    """Wait for one complete run of the health-sync timer's job from ``package``.

    After `daemon-reload` the timer keeps firing, but with the newly linked
    release's command: that is the mixed window's one unattended writer.
    """
    before = prop("InvocationID", HEALTH)
    deadline = time.monotonic() + 90
    while True:
        current = prop("InvocationID", HEALTH)
        if current not in ("", before) and prop("ActiveState", HEALTH) == "inactive":
            break
        assert time.monotonic() < deadline, f"no health sync ran ({before} -> {current})"
        time.sleep(1)
    assert prop("Result", HEALTH) == "success", prop("Result", HEALTH)
    command = prop("ExecStart", HEALTH)
    assert f"path={package}/bin/wall-in-one " in command, command


def log_lines() -> list[str]:
    return machine.succeed(f"cat {DRIVER_LOG} 2>/dev/null || true").splitlines()


def applied_by(pid: str, since: int) -> str | None:
    """A wallpaper Noctalia showed for service ``pid`` (after line ``since``)."""
    for line in log_lines()[since:]:
        parent, status, command = line.split("\t", 2)
        if parent == pid and status == "0" and command.startswith("msg wallpaper-set "):
            return command
    return None


def wait_applied(pid: str, since: int) -> str:
    """The service ``pid`` had Noctalia show a wallpaper (after line ``since``)."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        command = applied_by(pid, since)
        if command is not None:
            return command
        time.sleep(0.5)
    raise AssertionError(f"service {pid} applied no wallpaper: {log_lines()[since:]}")


def wait_first_apply(package: str) -> tuple[str, str]:
    """The running service from ``package`` that has applied a wallpaper.

    At login the service can start before Noctalia answers. If the shell is
    not ready within the release's first-apply window (8 s in v0.1.4) the
    service exits and systemd starts it again, so follow it to the instance
    that succeeds rather than holding on to the first PID.
    """
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pid = prop("MainPID")
        if pid.isdigit() and int(pid) > 1:
            code, executable = machine.execute(f"readlink -e /proc/{pid}/exe")
            if code == 0 and executable.strip() == f"{package}/bin/wall-in-one-service":
                command = applied_by(pid, 0)
                if command is not None:
                    return pid, command
        time.sleep(1)
    raise AssertionError(f"no {package} service applied a wallpaper: {log_lines()}")


def cursor() -> str:
    return (
        machine.succeed("journalctl -n 0 --show-cursor --no-pager").split("-- cursor: ")[-1].strip()
    )


def journal(unit: str, after: str = "") -> str:
    since = f" --after-cursor={q(after)}" if after else ""
    return machine.succeed(f"journalctl -b --no-pager -o cat _SYSTEMD_USER_UNIT={unit}{since}")


def package_python(package: str, code: str) -> str:
    """Run ``code`` against ``package``'s installed modules, not a checkout."""
    prologue = (
        "import wall_in_one\n"
        f"assert wall_in_one.__file__.startswith({package + '/'!r}), wall_in_one.__file__\n"
    )
    return run(f"PYTHONPATH={package}/{SITE_PACKAGES} {PACKAGE_PYTHON} -c {q(prologue + code)}")


# -- snapshots and expectations ---------------------------------------------------------


def _nodes(text: str) -> dict[str, Any]:
    return vm_tool.nodes(json.loads(text))


#: Lists the app's lock files (the profile lock, each store's, the compiler's)
#: that some process holds. It takes and drops each flock without creating,
#: writing or touching any file.
BUSY_LOCKS = """
import fcntl, os, sys
from pathlib import Path
for directory in sys.argv[1:]:
    for lock in sorted(Path(directory).glob("*.lock")):
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(lock.name)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
"""


def wait_profile_idle(timeout: float = 90) -> None:
    """No app writer holds a profile lock: nothing is mid-transaction."""
    deadline = time.monotonic() + timeout
    while busy := machine.succeed(
        f"{GUEST_PYTHON} -c {q(BUSY_LOCKS)} {q(STATE)} {q(HOME + '/.config/wall-in-one')}"
    ).split():
        if time.monotonic() > deadline:
            holders = machine.succeed("ps -e -o pid=,sid=,args= | grep -F wall-in-one || true")
            raise AssertionError(f"still held after {timeout:g} s: {busy}\n{holders}")
        time.sleep(0.2)


def snapshot() -> dict[str, Any]:
    """The profile once no app writer is mid-transaction."""
    wait_profile_idle()
    return _nodes(
        machine.succeed(
            f"PYTHONPATH={SUPPORT} PYTHONDONTWRITEBYTECODE=1 {GUEST_PYTHON} "
            f"{SUPPORT}/vm_tool.py snapshot --home {HOME}"
        )
    )


def writes(
    label: str, before: dict[str, Any], after: dict[str, Any], *, shell_first_start: bool = False
) -> list[Any]:
    """Every write between two snapshots, for the phase's own allowances.

    Noctalia's own rewrites are judged here, in every phase, and set aside:
    its settings.toml may change only in vm_tool.SHELL_FIELDS (plus, across
    the shell's first start, vm_tool.SHELL_MIGRATION), so an app -- or anyone
    -- changing any other key fails whichever phase it happens in.
    """
    changes = harness.diff(before, after)
    observations[label] = [change.describe() for change in changes]
    print(f"WIO_WRITES {label}: {json.dumps(observations[label], indent=1)}")
    rest, problems = vm_tool.shell_writes(changes, first_start=shell_first_start)
    expect(
        f"{label}: Noctalia's settings change only in shell-owned fields",
        lambda: require(not problems, "; ".join(problems)),
    )
    return rest


def expect(label: str, check: Callable[[], None]) -> None:
    try:
        check()
    except AssertionError as error:
        message = f"{label}: {error}"
        failures.append(message)
        print(f"WIO_EXPECTATION_FAILED {message}")
        if STRICT:
            raise


def require(condition: bool, message: str) -> None:
    assert condition, message


def observe(label: str, value: Any) -> None:
    observations[label] = value
    print(f"WIO_OBSERVED {label}: {json.dumps(value, indent=1, sort_keys=True, default=str)}")


def content(nodes: dict[str, Any], name: str) -> bytes:
    data = nodes[f"{STATE_REL}/{name}"].content
    assert isinstance(data, bytes), name
    return data


def document(nodes: dict[str, Any], name: str) -> dict[str, Any]:
    value = json.loads(content(nodes, name))
    assert isinstance(value, dict)
    return value


#: Written by 0.3.x and earlier, when the service asked niri for displays. The
#: service accepts and ignores it, so a document with it means the same as one
#: without, and the first 0.4 start recompiles it away.
RETIRED_RENDERER_KEYS = ("niri_program",)

#: The programs runtime.toml names. Where each is depends on the release that
#: compiled it: 0.1.4 resolved them into the store (its own wrapper's bundled
#: renderers, the probe first on PATH), 0.4 names the profile path it finds
#: them in (docs/packaging.md). The program, its basename, must not change.
PROGRAM_KEYS = ("noctalia_program", "mpvpaper_program", "linux_wallpaperengine_program")


def runtime_meaning(data: bytes) -> dict[str, Any]:
    value = tomllib.loads(data.decode())
    value.pop("config_generation", None)
    renderer = value.get("renderer")
    if isinstance(renderer, dict):
        for key in RETIRED_RENDERER_KEYS:
            renderer.pop(key, None)
        for key in PROGRAM_KEYS:
            if isinstance(renderer.get(key), str):
                renderer[key] = renderer[key].rsplit("/", 1)[-1]
    return value


def programs(data: bytes) -> dict[str, Any]:
    renderer = tomllib.loads(data.decode())["renderer"]
    return {key: renderer.get(key) for key in PROGRAM_KEYS}


#: Where this build may name a program in this VM: the Nix profiles the
#: compiler looks in (the module installs into the system one). Not
#: /usr/bin/<name>, the placeholder for a program it did not find.
PROFILE_BIN_DIRECTORIES = (
    f"{HOME}/.nix-profile/bin",
    f"{HOME}/.local/state/nix/profile/bin",
    "/etc/profiles/per-user/wallpaper/bin",
    "/nix/var/nix/profiles/default/bin",
    "/run/current-system/sw/bin",
)


def same_program(change: Any) -> None:
    """A recompile by another release may change the generation, not the meaning.

    Nor the programs: only the directory each is named in, as the golden
    profile's own comparison allows (tests/golden/harness.py), and that
    directory must be a profile's, so a recompile that fell back to the
    /usr/bin placeholder does not pass for the same program.
    """
    before = runtime_meaning(change.before.content)
    after = runtime_meaning(change.after.content)
    assert before == after, "runtime.toml changed meaning"
    for key, program in programs(change.after.content).items():
        assert isinstance(program, str) and program.rsplit("/", 1)[0] in (
            PROFILE_BIN_DIRECTORIES
        ), f"runtime.toml names {key} = {program!r}, not a profile's"


def unchanged(before: dict[str, Any], after: dict[str, Any], name: str) -> None:
    old, new = before.get(f"{STATE_REL}/{name}"), after.get(f"{STATE_REL}/{name}")
    assert old is not None and new is not None, f"{name} is missing"
    assert (old.digest, old.inode, old.mtime_ns) == (new.digest, new.inode, new.mtime_ns), (
        f"{name} was written"
    )


def schema(nodes: dict[str, Any]) -> int:
    value = tomllib.loads(content(nodes, "runtime.toml").decode())["schema_version"]
    assert isinstance(value, int)
    return value


def rendered_palette(change: Any) -> None:
    value = json.loads(change.after.content)
    assert isinstance(value, dict) and isinstance(value.get("colors"), dict), value


#: Not an app write. The golden profile's Noctalia settings register the user
#: template whose output is palette.json, so Noctalia renders it whenever a
#: service applies a wallpaper. Allowed only across a wallpaper application.
NOCTALIA_PALETTE = harness.Allowance(
    f"{STATE_REL}/palette.json",
    frozenset({"modified", "rewritten"}),
    "Noctalia renders the profile's registered template on a wallpaper change",
    rendered_palette,
)


def first_start() -> list[Any]:
    return [
        harness.Allowance(
            RUNTIME_REL,
            frozenset({"modified", "rewritten"}),
            "the first start of a release that did not compile it",
            same_program,
        ),
        NOCTALIA_PALETTE,
    ]


# -- the scenario ---------------------------------------------------------------------

start_all()
machine.wait_for_unit("multi-user.target")
machine.wait_for_unit("cage-tty1.service")
machine.wait_for_file(f"{RUNTIME_DIR}/bus")
machine.wait_until_succeeds(f"test -S {RUNTIME_DIR}/wayland-1", timeout=60)
machine.wait_until_succeeds(user("systemctl --user is-active wall-in-one.service"), timeout=90)
machine.wait_until_succeeds(user("systemctl --user is-active noctalia.service"), timeout=90)
machine.wait_for_file(RUNTIME_SOCKET)
seeded = _nodes(machine.succeed("cat /var/lib/wall-in-one-upgrade/seed.json"))

with subtest("a: v0.1.4 starts on the golden profile and applies a wallpaper"):
    check_loaded(OLD)
    pid, applied = wait_first_apply(OLD)
    observe("a: applied", applied)
    started = wait_status(
        OLD, lambda value: value["playlist_id"] == EVENINGS and value["last_error"] == ""
    )
    observe("a: v0.1.4 status", started)
    assert check_running(OLD) == pid
    assert started["runtime_executable"] == f"{OLD}/bin/wall-in-one-service", started
    # A service that never mentions it never reads runtime-overrides.toml.
    assert "supported_override_schemas" not in started, started
    a_done = snapshot()
    # The shipped release's own first start: recorded, not judged.
    # Noctalia's first start on the fixture's settings happens here too.
    writes("a: v0.1.4 first start", seeded, a_done, shell_first_start=True)
    observe("a: service journal", journal("wall-in-one.service"))

with subtest("b: v0.3.0 installed; the mixed window, then its first start and idle"):
    b_start = snapshot()
    old_pid = check_running(OLD)
    install(NEW)
    assert check_running(OLD) == old_pid
    mixed = status(NEW)
    assert mixed["runtime_executable"] == f"{OLD}/bin/wall-in-one-service", mixed
    assert mixed["runtime_instance"] == started["runtime_instance"], mixed
    observe("b: --update-status in the mixed window", run(f"{NEW}/bin/wall-in-one --update-status"))
    wait_health_sync(NEW)
    b_window = snapshot()
    window_writes = writes("b: mixed window", b_start, b_window)
    expect("b: the mixed window writes nothing", lambda: harness.check_changes(window_writes, ()))

    since, mark = len(log_lines()), cursor()
    restart()
    new_pid = check_running(NEW)
    updated = wait_status(
        NEW,
        lambda value: value["runtime_executable"] == f"{NEW}/bin/wall-in-one-service"
        and value["playlist_id"] == EVENINGS,
    )
    observe("b: v0.3.0 status", updated)
    assert "supported_override_schemas" in updated, updated
    assert updated["loaded_overrides_sha256"] is None, updated
    assert updated["cycle_interval_seconds"] == GLOBAL_INTERVAL, updated
    observe("b: applied", wait_applied(new_pid, since))
    wait_health_sync(NEW)
    b_first = snapshot()
    observe("b: service journal", journal("wall-in-one.service", mark))
    first_writes = writes("b: v0.3.0 first start and idle", b_window, b_first)
    expect(
        "b: the first start writes only the whitelist",
        lambda: harness.check_changes(first_writes, first_start()),
    )
    # 0.1.4 named store paths (its own bundled renderers, the probe on PATH);
    # this build names what the module and the probe put in the system
    # profile, which survives the next switch and collection.
    expect(
        "b: runtime.toml names the system profile's programs",
        lambda: require(
            programs(content(b_first, "runtime.toml"))
            == {
                "noctalia_program": "/run/current-system/sw/bin/noctalia",
                "mpvpaper_program": "/run/current-system/sw/bin/mpvpaper",
                "linux_wallpaperengine_program": "/run/current-system/sw/bin/linux-wallpaperengine",
            },
            f"runtime.toml names {programs(content(b_first, 'runtime.toml'))}",
        ),
    )
    # Directly, not only through the allowance's callback, which runs only if
    # runtime.toml happened to be rewritten.
    expect(
        "b: runtime.toml still means what v0.1.4 compiled",
        lambda: require(
            runtime_meaning(content(b_first, "runtime.toml"))
            == runtime_meaning(content(b_window, "runtime.toml")),
            "runtime.toml changed meaning",
        ),
    )
    wait_health_sync(NEW)
    b_idle = snapshot()
    idle_writes = writes("b: v0.3.0 idle again", b_first, b_idle)
    expect("b: a second idle writes nothing", lambda: harness.check_changes(idle_writes, ()))

with subtest("c: v0.3.0 uses a per-playlist interval, a rule name and the display opt-in"):
    c_start = snapshot()
    released = {name: content(c_start, name) for name in STORES}
    assert document(c_start, "displays.json")["displays"].get(OPT_IN), "the fixture assigns DP-1"
    package_python(
        NEW,
        "from wall_in_one.library import displays, playlists, schedules\n"
        f"playlists.Store.open().set_rotation({EVENINGS!r}, cycle_interval={INTERVAL})\n"
        f"schedules.Store.open().set_name({NAMED_RULE!r}, {RULE_NAME!r})\n"
        f"assert displays.Store.open().set_beats_global_rules({OPT_IN!r}, True)\n",
    )
    observe("c: --write-config", run(f"{NEW}/bin/wall-in-one --write-config"))
    observe("c: reload", ctl(NEW, "reload"))
    sidecar_sha = machine.succeed(f"sha256sum {OVERRIDES}").split()[0]
    applied_overrides = wait_status(
        NEW,
        lambda value: value.get("loaded_overrides_sha256") == sidecar_sha
        and value.get("cycle_interval_seconds") == INTERVAL,
    )
    observe("c: v0.3.0 status with the overrides", applied_overrides)
    c_done = snapshot()
    c_writes = writes("c: v0.3.0 edits", c_start, c_done)

    def bumped(name: str, version: int, check: Callable[[dict[str, Any]], None]) -> Any:
        def verify(change: Any) -> None:
            after = json.loads(change.after.content)
            assert after["version"] == version, after["version"]
            check(after)

        return verify

    def rotation(after: dict[str, Any]) -> None:
        before = json.loads(released["playlists.json"])
        found = {playlist["id"]: playlist for playlist in after["playlists"]}
        assert found[EVENINGS].get("cycle_interval") == INTERVAL, found[EVENINGS]
        for playlist in before["playlists"]:
            mine = dict(found[playlist["id"]])
            if playlist["id"] == EVENINGS:
                mine.pop("cycle_interval")
            assert mine == playlist, playlist["id"]

    def named(after: dict[str, Any]) -> None:
        before = json.loads(released["schedules.json"])
        found = {rule["id"]: rule for rule in after["rules"]}
        assert found[NAMED_RULE].get("name") == RULE_NAME, found[NAMED_RULE]
        for rule in before["rules"]:
            mine = dict(found[rule["id"]])
            if rule["id"] == NAMED_RULE:
                mine.pop("name")
            assert mine == rule, rule["id"]

    def opted_in(after: dict[str, Any]) -> None:
        before = json.loads(released["displays.json"])
        assert after["displays"] == before["displays"], after
        assert after["beats_global_rules"] == [OPT_IN], after
        assert set(after) == {"version", "displays", "beats_global_rules"}, after

    def overrides(change: Any) -> None:
        value = tomllib.loads(change.after.content.decode())
        assert value["playlists"] == [{"id": EVENINGS, "cycle_interval_seconds": INTERVAL}], value
        # Mirrored, the display opt-in stays dormant in its store.
        assert "displays" not in value, value

    c_allowed = [
        harness.Allowance(
            f"{STATE_REL}/playlists.json",
            frozenset({"modified"}),
            "a per-playlist interval: playlists.json 1 -> 2",
            bumped("playlists.json", 2, rotation),
        ),
        harness.Allowance(
            f"{STATE_REL}/schedules.json",
            frozenset({"modified"}),
            "a rule name: schedules.json 2 -> 3",
            bumped("schedules.json", 3, named),
        ),
        harness.Allowance(
            f"{STATE_REL}/displays.json",
            frozenset({"modified"}),
            "the own-playlist-beats-global-rules opt-in: displays.json 1 -> 2",
            bumped("displays.json", 2, opted_in),
        ),
        harness.Allowance(
            OVERRIDES_REL,
            frozenset({"created"}),
            "the new runtime fields, beside an unchanged runtime.toml",
            overrides,
        ),
    ]
    # A bump is an ordinary save: a copy of the old bytes beside the file would
    # be an unexpected write here.
    expect(
        "c: exactly the bumps and the overrides file",
        lambda: harness.check_changes(c_writes, c_allowed),
    )
    # check_changes only vets the writes that happened; require all four, so
    # every verifier above has run on its file.
    expect(
        "c: every bump and the overrides file was written",
        lambda: require(
            sorted((change.path, change.kind) for change in c_writes)
            == sorted((allowance.pattern, next(iter(allowance.kinds))) for allowance in c_allowed),
            str(sorted((change.path, change.kind) for change in c_writes)),
        ),
    )
    expect("c: runtime.toml keeps its shape", lambda: unchanged(c_start, c_done, "runtime.toml"))
    assert schema(c_done) <= 5, schema(c_done)

with subtest("c2: this build's service restarts on the profile c left"):
    # The upgrade direction on a profile with bumped stores and an overrides
    # file: the packaged service starts again, loads the overrides, applies a
    # wallpaper and syncs health, and the start writes only the first-start
    # whitelist. Nothing c wrote is lost, directly: the stores, the overrides
    # and runtime.toml are byte for byte what c left.
    c2_start = snapshot()
    since, mark = len(log_lines()), cursor()
    restart()
    new_pid = check_running(NEW)
    restarted = wait_status(
        NEW,
        lambda value: (
            value["runtime_executable"] == f"{NEW}/bin/wall-in-one-service"
            and value.get("loaded_overrides_sha256") == sidecar_sha
            and value.get("cycle_interval_seconds") == INTERVAL
        ),
    )
    observe("c2: status after the restart", restarted)
    observe("c2: applied", wait_applied(new_pid, since))
    wait_health_sync(NEW)
    c2_done = snapshot()
    observe("c2: service journal", journal("wall-in-one.service", mark))
    c2_writes = writes("c2: restart and idle", c2_start, c2_done)
    expect(
        "c2: the start writes only the first-start whitelist",
        lambda: harness.check_changes(c2_writes, first_start()),
    )
    # runtime.toml too: first_start() would allow it to be rewritten, but this
    # build compiled the same document, so a restart must not touch the file.
    for kept in (*STORES, "runtime-overrides.toml", "runtime.toml"):
        expect(f"c2: {kept} is as c left it", partial(unchanged, c_done, c2_done, kept))

with subtest("the session stayed healthy"):
    machine.fail("coredumpctl --json=short | grep -E 'wall-in-one|niri|noctalia'")
    tracebacks = machine.execute(
        "journalctl -b --no-pager | grep -E 'Traceback|thread .* panicked|Gtk-CRITICAL'"
    )[1]
    observe("journal tracebacks", tracebacks)
    expect("no tracebacks or panics", lambda: require(not tracebacks.strip(), tracebacks))

print("WIO_OBSERVATIONS " + json.dumps(observations, sort_keys=True, default=str))
assert not failures, failures
