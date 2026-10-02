# The upgrade-rollback scenario, run by the NixOS test driver.
#
# upgrade-rollback-test.nix prepends the constants it uses (OLD, NEW, HOME,
# RUNTIME_DIR, PROFILE, DRIVER_LOG, NOCTALIA_PROBE, SUPPORT, GUEST_PYTHON, PACKAGE_PYTHON,
# SITE_PACKAGES, BASH, NIRI, STRICT). `machine`, `start_all` and `subtest`
# are the driver's. Every write to the profile is judged with
# tests/golden/harness.py's own diff and allowance machinery, on snapshots the
# guest takes of the trees Wall-in-One owns or reads.

import importlib
import json
import re
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
GUI_SOCKET = f"{RUNTIME_DIR}/wall-in-one.sock"
HEALTH = "wall-in-one-health-sync.service"
APP_ID = "dev.goober.WallInOne"
#: The fixture's default playlist ("Evenings", all video), which plays at the
#: seeded clock because no timed rule is active then.
EVENINGS = "bd7adc21870f0664"
TRAVEL = "a545a472f65dd73b"
#: The enabled Evenings rule, 21:00-04:00.
NAMED_RULE = "af5d9a02e281e683"
RULE_NAME = "Evening lights"
#: Assigned in the fixture's displays.json, so the opt-in is meaningful.
OPT_IN = "DP-1"
INTERVAL = 120
GLOBAL_INTERVAL = 900
PICTURE = f"{HOME}/Pictures/Wallpapers/meadow-light.png"
STORES = ("playlists.json", "schedules.json", "displays.json")
SAFE_PRE_ADMISSION = {
    "current-profile authoring is still opening; retry shortly",
    "another authoring change is still being saved; retry after it finishes",
}

failures: list[str] = []
observations: dict[str, Any] = {}


# -- the guest -------------------------------------------------------------------------


def user(command: str) -> str:
    environment = (
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
    return f"runuser -u wallpaper -- env -i {environment} {BASH} -euo pipefail -c {q(command)}"


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


def niri(arguments: str) -> str:
    return run(
        f"NIRI_SOCKET=$(find {RUNTIME_DIR} -maxdepth 1 -name 'niri*.sock' -print -quit) "
        f"{NIRI} msg {arguments}"
    )


def windows() -> list[dict[str, Any]]:
    return [window for window in json.loads(niri("--json windows")) if window.get("app_id") == APP_ID]


def open_gui(package: str, page: str, title: str) -> dict[str, Any]:
    """`ctl open`, as vm-test.nix does: the authoring verbs need the GTK app."""
    assert ctl(package, f"open {page}") == f"launch requested for {page}"
    machine.wait_until_succeeds(
        user(
            f"NIRI_SOCKET=$(find {RUNTIME_DIR} -maxdepth 1 -name 'niri*.sock' -print -quit) "
            f"{NIRI} msg windows | grep -F {q('Wall-in-One - ' + title)}"
        ),
        timeout=90,
    )
    (window,) = windows()
    command = machine.succeed(f"tr '\\0' ' ' < /proc/{window['pid']}/cmdline")
    assert package in command, command
    return window


def close_gui() -> None:
    for window in windows():
        niri(f"action close-window --id {window['id']}")
        machine.wait_until_succeeds(f"test ! -e /proc/{window['pid']}", timeout=60)
    machine.wait_until_succeeds(f"test ! -e {GUI_SOCKET}", timeout=30)
    assert not windows()


def gui_library(package: str) -> tuple[int, int, list[str]]:
    """The open GUI's library, from `ctl list`: rows listed, the library's size, the paths.

    v0.1.4's GUI serves `list` from the scan it is showing:
    ``# library: <listed> of <size> wallpapers``, then one tab-separated row
    per wallpaper, its path first.
    """
    output = ctl(package, "list")
    lines = output.splitlines()
    match = re.match(r"# library: (\d+) of (\d+) ", lines[0]) if lines else None
    assert match, output
    paths = sorted(line.split("\t", 1)[0] for line in lines if not line.startswith("# "))
    return int(match[1]), int(match[2]), paths


def settle_authoring(package: str, arguments: str) -> tuple[int, str]:
    """One authoring verb's outcome once the GUI's startup gate has settled."""
    command = user(f"{package}/bin/wall-in-one ctl {arguments} 2>&1")
    for _attempt in range(600):
        code, output = machine.execute(command)
        if code == 0 or output.strip() not in SAFE_PRE_ADMISSION:
            return code, output.strip()
        time.sleep(0.2)
    raise AssertionError(f"the authoring gate never settled: {arguments}")


# -- snapshots and expectations ---------------------------------------------------------


def _nodes(text: str) -> dict[str, Any]:
    return vm_tool.nodes(json.loads(text))


def snapshot() -> dict[str, Any]:
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


def runtime_meaning(data: bytes) -> dict[str, Any]:
    value = tomllib.loads(data.decode())
    value.pop("config_generation", None)
    return value


def same_program(change: Any) -> None:
    """A recompile by another release may change the generation, not the meaning.

    Both releases resolve their renderers from the same nixpkgs through the
    same wrapper PATH, so the program paths must match too.
    """
    before = runtime_meaning(change.before.content)
    after = runtime_meaning(change.after.content)
    assert before == after, "runtime.toml changed meaning"


def unchanged(before: dict[str, Any], after: dict[str, Any], name: str) -> None:
    old, new = before.get(f"{STATE_REL}/{name}"), after.get(f"{STATE_REL}/{name}")
    assert old is not None and new is not None, f"{name} is missing"
    assert (old.digest, old.inode, old.mtime_ns) == (new.digest, new.inode, new.mtime_ns), (
        f"{name} was written"
    )


def broken_copies(nodes: dict[str, Any], name: str) -> list[str]:
    prefix = f"{STATE_REL}/{name}.broken"
    return sorted(path for path in nodes if path.startswith(prefix))


def compiled_stills(nodes: dict[str, Any], playlist_id: str) -> list[str]:
    """The stills runtime.toml compiles for one playlist, in order."""
    document = tomllib.loads(content(nodes, "runtime.toml").decode())
    (playlist,) = (entry for entry in document["playlists"] if entry["id"] == playlist_id)
    return [entry.get("still", "") for entry in playlist.get("entries", [])]


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

with subtest("b: v0.2.0 installed; the mixed window, then its first start and idle"):
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
    observe("b: v0.2.0 status", updated)
    assert "supported_override_schemas" in updated, updated
    assert updated["loaded_overrides_sha256"] is None, updated
    assert updated["cycle_interval_seconds"] == GLOBAL_INTERVAL, updated
    observe("b: applied", wait_applied(new_pid, since))
    wait_health_sync(NEW)
    b_first = snapshot()
    observe("b: service journal", journal("wall-in-one.service", mark))
    first_writes = writes("b: v0.2.0 first start and idle", b_window, b_first)
    expect(
        "b: the first start writes only the whitelist",
        lambda: harness.check_changes(first_writes, first_start()),
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
    idle_writes = writes("b: v0.2.0 idle again", b_first, b_idle)
    expect("b: a second idle writes nothing", lambda: harness.check_changes(idle_writes, ()))

with subtest("c: v0.2.0 uses a per-playlist interval, a rule name and the display opt-in"):
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
    observe("c: v0.2.0 status with the overrides", applied_overrides)
    c_done = snapshot()
    c_writes = writes("c: v0.2.0 edits", c_start, c_done)

    def backup_of(name: str) -> Callable[[Any], None]:
        def verify(change: Any) -> None:
            assert change.after.content == released[name], f"not the released {name}"

        return verify

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
            f"{STATE_REL}/playlists.json.v1-backup",
            frozenset({"created"}),
            "the one-time backup of the released playlists.json",
            backup_of("playlists.json"),
        ),
        harness.Allowance(
            f"{STATE_REL}/schedules.json",
            frozenset({"modified"}),
            "a rule name: schedules.json 2 -> 3",
            bumped("schedules.json", 3, named),
        ),
        harness.Allowance(
            f"{STATE_REL}/schedules.json.v2-backup",
            frozenset({"created"}),
            "the one-time backup of the released schedules.json (version 2)",
            backup_of("schedules.json"),
        ),
        harness.Allowance(
            f"{STATE_REL}/displays.json",
            frozenset({"modified"}),
            "the own-playlist-beats-global-rules opt-in: displays.json 1 -> 2",
            bumped("displays.json", 2, opted_in),
        ),
        harness.Allowance(
            f"{STATE_REL}/displays.json.v1-backup",
            frozenset({"created"}),
            "the one-time backup of the released displays.json",
            backup_of("displays.json"),
        ),
        harness.Allowance(
            OVERRIDES_REL,
            frozenset({"created"}),
            "the new runtime fields, beside an unchanged runtime.toml",
            overrides,
        ),
    ]
    expect(
        "c: exactly the bumps, their backups and the overrides file",
        lambda: harness.check_changes(c_writes, c_allowed),
    )
    # check_changes only vets the writes that happened; require all seven, so
    # every verifier above has run on its file.
    expect(
        "c: every bump, backup and the overrides file was written",
        lambda: require(
            sorted((change.path, change.kind) for change in c_writes)
            == sorted((allowance.pattern, next(iter(allowance.kinds))) for allowance in c_allowed),
            str(sorted((change.path, change.kind) for change in c_writes)),
        ),
    )
    expect("c: runtime.toml keeps its shape", lambda: unchanged(c_start, c_done, "runtime.toml"))
    assert schema(c_done) <= 5, schema(c_done)

with subtest("d: v0.1.4 installed again; the mixed window, then its restart"):
    d_start = snapshot()
    new_pid = check_running(NEW)
    install(OLD)
    assert check_running(NEW) == new_pid
    mixed = status(OLD)
    observe("d: v0.1.4 ctl status against v0.2.0", mixed)
    assert mixed["runtime_executable"] == f"{NEW}/bin/wall-in-one-service", mixed
    wait_health_sync(OLD)
    d_window = snapshot()
    window_writes = writes("d: mixed window", d_start, d_window)
    expect("d: the mixed window writes nothing", lambda: harness.check_changes(window_writes, ()))

    since, mark = len(log_lines()), cursor()
    restart()
    old_pid = check_running(OLD)
    rolled_back = wait_status(
        OLD,
        lambda value: value["runtime_executable"] == f"{OLD}/bin/wall-in-one-service"
        and value["playlist_id"] == EVENINGS,
    )
    observe("d: v0.1.4 status after the rollback", rolled_back)
    assert "supported_override_schemas" not in rolled_back, rolled_back
    prepared = journal("wall-in-one.service", mark)
    observe("d: service journal", prepared)
    expect(
        "d: v0.1.4's preflight keeps the last runtime.toml",
        lambda: require(
            "could not be compiled" in prepared
            and "the existing runtime configuration was left untouched" in prepared
            and "playlists.json has unsupported version 2" in prepared
            and "schedules.json has unsupported version 3" in prepared
            and "displays.json has unsupported version 2" in prepared,
            "no last-known-good refusal naming all three bumped stores",
        ),
    )
    run(f"{OLD}/bin/wall-in-one-service --check-config --config {RUNTIME}")
    observe("d: applied", wait_applied(old_pid, since))
    wait_health_sync(OLD)
    d_done = snapshot()
    restart_writes = writes("d: v0.1.4 restart and idle", d_window, d_done)
    expect(
        "d: the rollback start writes nothing of the app's",
        lambda: harness.check_changes(restart_writes, [NOCTALIA_PALETTE]),
    )
    assert schema(d_done) <= 5

with subtest("e1: v0.1.4's own GUI refuses to edit the bumped profile"):
    # 0.1.4 has no headless authoring: every authoring ctl verb is served by
    # the GTK app's socket, `--write-config` only compiles, and `--service` is
    # the legacy renderer. So the real path is `ctl open` + `ctl playlist-add`,
    # as vm-test.nix drives it. Its authoring gate first checks references in
    # all three stores; a store it reads as an unsupported version fails that
    # check and leaves every write paused (and the library unloaded) -- not
    # only playlist, schedule and display edits but favourites too.
    e_start = snapshot()
    open_gui(OLD, "playlists", "Playlists")
    for verb in (f"playlist-add {TRAVEL} {PICTURE}", f"favourite {PICTURE}"):
        code, refused = settle_authoring(OLD, verb)
        observe(f"e1: v0.1.4 ctl {verb.split()[0]}", {"exit": code, "output": refused})
        assert code == 1, (verb, code, refused)
        assert "current authoring needs repair before changes can be saved" in refused, refused
    # What the paused app shows: recorded for docs/updating.md, not judged.
    code, listed = machine.execute(user(f"{OLD}/bin/wall-in-one ctl list 2>&1"))
    observe("e1: v0.1.4 ctl list", {"exit": code, "output": listed.strip()})
    machine.screenshot("rollback-v0.1.4-playlists")
    close_gui()
    e_gui = snapshot()
    gui_writes = writes("e1: v0.1.4 GUI open, refused edit, close", e_start, e_gui)
    expect("e1: the refused edit writes nothing", lambda: harness.check_changes(gui_writes, ()))

with subtest("e2: v0.2.0 installed again finds the bumped profile as it left it"):
    e2_start = snapshot()
    old_pid = check_running(OLD)
    install(NEW)
    assert check_running(OLD) == old_pid
    since, mark = len(log_lines()), cursor()
    restart()
    new_pid = check_running(NEW)
    again = wait_status(
        NEW,
        lambda value: value["runtime_executable"] == f"{NEW}/bin/wall-in-one-service"
        and value.get("loaded_overrides_sha256") == sidecar_sha
        and value.get("cycle_interval_seconds") == INTERVAL,
    )
    observe("e2: v0.2.0 status after the raw rollback", again)
    observe("e2: applied", wait_applied(new_pid, since))
    wait_health_sync(NEW)
    e2_done = snapshot()
    e2_writes = writes("e2: v0.2.0 start after the raw rollback", e2_start, e2_done)
    expect(
        "e2: the start writes only the first-start whitelist",
        lambda: harness.check_changes(e2_writes, first_start()),
    )
    # Nothing was lost, directly: the stores, the overrides and the backups
    # are byte for byte what c left.
    for kept in (*STORES, "runtime-overrides.toml", *(f"{name}.v1-backup" for name in ("playlists.json", "displays.json")), "schedules.json.v2-backup"):
        expect(f"e2: {kept} is as c left it", partial(unchanged, c_done, e2_done, kept))


def check_narrowed(nodes: dict[str, Any], name: str) -> None:
    """The tool's rewrite: 0.1.4's version, every record of c's file, minus the new fields."""
    before = document(c_done, name)
    after = document(nodes, name)
    if name == "playlists.json":
        assert after["version"] == 1, after["version"]
        assert after["playlists"] == [
            {key: value for key, value in playlist.items() if key not in ("cycle_interval", "shuffle")}
            for playlist in before["playlists"]
        ], "a playlist or entry changed"
    elif name == "schedules.json":
        assert after["version"] == 2, after["version"]
        assert after["rules"] == [
            {key: value for key, value in rule.items() if key != "name"} for rule in before["rules"]
        ], "a rule changed"
    else:
        assert after == {"version": 1, "displays": before["displays"]}, after


with subtest("f: wall-in-one-rollback rewrites the three files for 0.1.4"):
    run("systemctl --user stop wall-in-one.service")
    machine.wait_until_fails(user("systemctl --user is-active wall-in-one.service"), timeout=60)
    f_start = snapshot()
    dry = run(f"{NEW}/bin/wall-in-one-rollback")
    observe("f: dry run", dry)
    for line in (
        "playlists.json: version 2 -> 1",
        '  playlist "Evenings" loses its interval (120 s)',
        "schedules.json: version 3 -> 2",
        f'  rule "{RULE_NAME}" loses its name',
        "displays.json: version 2 -> 1",
        f"  display {OPT_IN} loses its own playlist beating global schedule rules",
    ):
        expect(f"f: the dry run says {line.strip()!r}", partial(require, line in dry.splitlines(), dry))
    expect(
        "f: the dry run writes nothing",
        lambda: harness.check_changes(writes("f: dry run", f_start, snapshot()), ()),
    )

    applied = run(f"{NEW}/bin/wall-in-one-rollback --apply")
    observe("f: --apply", applied)
    f_done = snapshot()
    (backup,) = sorted(
        path.rsplit("/", 1)[-1]
        for path in f_done
        if path.startswith(f"{STATE_REL}/rollback-to-0.1.4-")
        and path.count("/") == STATE_REL.count("/") + 1
    )
    backup_rel = f"{STATE_REL}/{backup}"
    backed_up_names = (*STORES, "runtime-overrides.toml")
    f_writes = writes("f: wall-in-one-rollback --apply", f_start, f_done)
    f_allowed = [
        *(
            harness.Allowance(
                f"{STATE_REL}/{name}",
                frozenset({"modified"}),
                f"wall-in-one-rollback rewrites {name} in 0.1.4's version, every record kept",
                lambda change, name=name: check_narrowed(f_done, name),
            )
            for name in STORES
        ),
        harness.Allowance(
            OVERRIDES_REL,
            frozenset({"deleted"}),
            "wall-in-one-rollback removes runtime-overrides.toml, which 0.1.4 never reads",
        ),
        harness.Allowance(
            backup_rel, frozenset({"created"}), "wall-in-one-rollback's one backup folder"
        ),
        *(
            harness.Allowance(
                f"{backup_rel}/{name}",
                frozenset({"created"}),
                f"wall-in-one-rollback's copy of {name}, made before anything is written",
            )
            for name in backed_up_names
        ),
    ]
    expect(
        "f: the three stores narrowed, the overrides removed, a backup first, nothing else",
        lambda: harness.check_changes(f_writes, f_allowed),
    )
    # Directly, not only through the callbacks: every one of those writes
    # happened, and the files hold what the tool promises.
    expect(
        "f: every rewrite, the removal and every backup copy happened",
        lambda: require(
            sorted((change.path, change.kind) for change in f_writes)
            == sorted((allowance.pattern, next(iter(allowance.kinds))) for allowance in f_allowed),
            str(sorted((change.path, change.kind) for change in f_writes)),
        ),
    )
    for name in STORES:
        expect(f"f: {name} is narrowed with every record", partial(check_narrowed, f_done, name))
    for name in backed_up_names:
        expect(
            f"f: the backup of {name} is the file as it was",
            partial(
                require,
                f_done[f"{backup_rel}/{name}"].content == content(f_start, name),
                f"the backup of {name} differs",
            ),
        )
    expect(
        "f: runtime-overrides.toml is gone",
        lambda: require(OVERRIDES_REL not in f_done, "runtime-overrides.toml is still there"),
    )
    for kept in ("runtime.toml", "pairings.json", "favourites.json", "playlists.json.v1-backup"):
        expect(f"f: {kept} is untouched", partial(unchanged, f_start, f_done, kept))
    expect(
        "f: it prints the two commands that put the backup back",
        lambda: require(
            "systemctl --user stop wall-in-one.service" in applied
            and f"cp -p -- {STATE}/{backup}/* {STATE}/" in applied,
            applied,
        ),
    )
    second = run(f"{NEW}/bin/wall-in-one-rollback --apply")
    observe("f: a second --apply", second)
    expect(
        "f: a second --apply finds nothing to do",
        partial(require, "Nothing to roll back" in second, second),
    )
    expect(
        "f: a second --apply writes nothing",
        lambda: harness.check_changes(writes("f: second --apply", f_done, snapshot()), ()),
    )


def all_media(nodes: dict[str, Any]) -> list[dict[str, Any]]:
    """runtime.toml's All media playlist: the whole library a compile scanned."""
    value = tomllib.loads(content(nodes, "runtime.toml").decode())
    (fallback,) = (playlist for playlist in value["playlists"] if playlist["id"] == "all-media")
    return list(fallback.get("entries", []))


def library_entries(nodes: dict[str, Any]) -> list[str]:
    """The ids in runtime.toml's All media playlist."""
    return sorted(entry["id"] for entry in all_media(nodes))


def library_files(nodes: dict[str, Any]) -> list[str]:
    """The file of every video and picture in All media (a scene is a directory)."""
    return sorted(
        entry["motion"] if entry["kind"] == "video" else entry["still"]
        for entry in all_media(nodes)
        if entry["kind"] in ("video", "still")
    )


with subtest("g: v0.1.4 runs the rewritten profile and edits it"):
    g_start = snapshot()
    install(OLD)
    since, mark = len(log_lines()), cursor()
    run("systemctl --user start wall-in-one.service")
    machine.wait_until_succeeds(user("systemctl --user is-active wall-in-one.service"), timeout=60)
    machine.wait_for_file(RUNTIME_SOCKET)
    old_pid = check_running(OLD)
    rolled = wait_status(
        OLD,
        lambda value: value["runtime_executable"] == f"{OLD}/bin/wall-in-one-service"
        and value["playlist_id"] == EVENINGS,
    )
    observe("g: v0.1.4 status on the rewritten profile", rolled)
    prepared = journal("wall-in-one.service", mark)
    observe("g: service journal", prepared)
    expect(
        "g: v0.1.4's service start compiles the rewritten files",
        lambda: require(
            "could not be compiled" not in prepared
            and (f"wrote: {RUNTIME}" in prepared or f"already current: {RUNTIME}" in prepared),
            "v0.1.4's preflight did not compile the rewritten stores",
        ),
    )
    observe("g: applied", wait_applied(old_pid, since))
    g_service = snapshot()
    writes("g: v0.1.4 start", g_start, g_service)
    expect(
        "g: the full library is in v0.1.4's runtime.toml",
        lambda: require(
            library_entries(g_service) == library_entries(c_done),
            f"{library_entries(g_service)} != {library_entries(c_done)}",
        ),
    )

    open_gui(OLD, "playlists", "Playlists")
    code, added = settle_authoring(OLD, f"playlist-add {TRAVEL} {PICTURE}")
    observe("g: v0.1.4 ctl playlist-add", {"exit": code, "output": added})
    expect("g: v0.1.4's playlist-add succeeds", partial(require, code == 0, added))
    # The app itself, not only the service's compile, has the whole library:
    # every wallpaper 0.2.0 compiled in c, the same count and every file.
    size = len(library_entries(c_done))
    deadline = time.monotonic() + 60
    while True:
        shown = gui_library(OLD)
        if shown[1] == size or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    observe("g: v0.1.4 ctl list", {"listed": shown[0], "of": shown[1], "paths": shown[2]})
    expect(
        "g: v0.1.4's app opens with the full library",
        lambda: require(
            shown[0] == shown[1] == size and set(library_files(c_done)) <= set(shown[2]),
            f"listed {shown[0]} of {shown[1]}, want {size} with {library_files(c_done)}",
        ),
    )
    machine.screenshot("rolled-back-v0.1.4-playlists")
    close_gui()
    g_done = snapshot()
    writes("g: v0.1.4 GUI edit and close", g_service, g_done)

    def edited() -> None:
        after = document(g_done, "playlists.json")
        travel = {playlist["id"]: playlist for playlist in after["playlists"]}[TRAVEL]
        assert travel["entries"][-1]["source"] == PICTURE, travel
        assert after["version"] == 1, after["version"]
        for name in STORES:
            assert broken_copies(g_done, name) == [], f"{name} was moved aside"

    expect("g: the edit is saved in place, with no .broken copy", edited)
    # Travel's own compiled entries: All media always lists the picture.
    expect(
        "g: v0.1.4 compiled its edit into runtime.toml",
        lambda: require(
            PICTURE not in compiled_stills(c_done, TRAVEL)
            and compiled_stills(g_done, TRAVEL)[-1:] == [PICTURE]
            and schema(g_done) <= 5,
            f"Travel compiles {compiled_stills(g_done, TRAVEL)}, schema {schema(g_done)}",
        ),
    )

with subtest("the session stayed healthy"):
    machine.fail("coredumpctl --json=short | grep -E 'wall-in-one|niri|noctalia'")
    tracebacks = machine.execute(
        "journalctl -b --no-pager | grep -E 'Traceback|thread .* panicked|Gtk-CRITICAL'"
    )[1]
    observe("journal tracebacks", tracebacks)
    expect("no tracebacks or panics", lambda: require(not tracebacks.strip(), tracebacks))

print("WIO_OBSERVATIONS " + json.dumps(observations, sort_keys=True, default=str))
assert not failures, failures
