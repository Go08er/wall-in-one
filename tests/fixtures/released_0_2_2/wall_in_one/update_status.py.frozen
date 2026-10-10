"""Read-only installed/running/loaded-generation evidence for update decisions.

Never reload the manager, start or stop a unit, or edit its files. An inactive
unit's metadata may need loading (as with systemctl show) before inspection.
These observations are not a handover lock or a release certificate.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wall_in_one import __version__, paths
from wall_in_one.control import client

BUS_NAME = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
UNIT_NAMES = ("wall-in-one.service", "wall-in-one-health-sync.service")
EXTRA_COMMANDS = ("ExecCondition", "ExecStartPost", "ExecStopPost", "ExecReload")
TIMEOUT = 5.0


@dataclass(frozen=True)
class Command:
    executable: str
    arguments: tuple[str, ...]
    ignore_errors: bool = False


@dataclass(frozen=True)
class Unit:
    name: str
    state: str
    fragment: str = ""
    drop_ins: tuple[str, ...] = ()
    needs_reload: bool = False
    main_pid: int = 0
    commands: dict[str, tuple[Command, ...]] = field(default_factory=dict)
    environment_keys: tuple[str, ...] = ()
    environment_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedUnits:
    state: str
    detail: str
    units: tuple[Unit, ...] = ()


def package_root() -> Path | None:
    """Find only this import's packaged executables, never the ambient PATH."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "share/systemd/user/wall-in-one.service").is_file() and (
            parent / "bin/wall-in-one-service"
        ).is_file():
            return parent
    return None


def _session_address() -> str | None:
    explicit = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if explicit:
        # Do not invoke D-Bus autolaunch (which can start unrelated services).
        if not explicit.startswith("unix:") or ";" in explicit:
            raise ValueError("the session bus address is not one local Unix endpoint")
        return explicit
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    directory = Path(runtime)
    if not directory.is_absolute():
        raise ValueError("XDG_RUNTIME_DIR is not absolute")
    try:
        info = (directory / "bus").stat()
    except FileNotFoundError:
        if (directory / "systemd/private").exists():
            raise ValueError("the user manager exists but its session bus is unavailable") from None
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("the session bus path is not this user's socket")
    # GLib's address escaping handles unusual but valid runtime directory names.
    from gi.repository import Gio

    return "unix:path=" + Gio.dbus_address_escape_value(str(directory / "bus"))


def _commands(value: object) -> tuple[Command, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("invalid loaded command inventory")
    result: list[Command] = []
    for row in value:
        if not isinstance(row, tuple | list) or len(row) != 10:
            raise ValueError("invalid loaded command record")
        executable, arguments = row[:2]
        if (
            not isinstance(executable, str)
            or type(row[2]) is not bool
            or not isinstance(arguments, list | tuple)
            or len(arguments) > 128
            or not all(isinstance(word, str) and len(word) <= 32768 for word in arguments)
        ):
            raise ValueError("invalid loaded command arguments")
        result.append(Command(executable, tuple(arguments), row[2]))
    return tuple(result)


def _read_units(address: str, timeout: float) -> tuple[Unit, ...] | None:
    """Read a single manager generation with a deadline including connection."""
    from gi.repository import Gio, GLib

    deadline = time.monotonic() + timeout
    cancelled = Gio.Cancellable()
    timer = threading.Timer(timeout, cancelled.cancel)
    timer.daemon = True
    timer.start()
    connection: Gio.DBusConnection | None = None
    try:
        connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            cancelled,
        )
        connection.set_exit_on_close(False)

        def call(
            destination: str, path: str, interface: str, method: str, args: GLib.Variant
        ) -> Any:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("service inspection deadline expired")
            assert connection is not None
            reply = connection.call_sync(
                destination,
                path,
                interface,
                method,
                args,
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                max(1, int(remaining * 1000)),
                cancelled,
            )
            return reply.unpack()[0]

        def owner() -> str | None:
            try:
                value = call(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "GetNameOwner",
                    GLib.Variant("(s)", (BUS_NAME,)),
                )
            except GLib.Error as error:
                if (
                    Gio.DBusError.get_remote_error(error)
                    == "org.freedesktop.DBus.Error.NameHasNoOwner"
                ):
                    return None
                raise
            if not isinstance(value, str) or not value.startswith(":"):
                raise ValueError("invalid service manager identity")
            return value

        manager = owner()
        if manager is None:
            return None

        def prop(path: str, interface: str, name: str) -> Any:
            return call(
                manager,
                path,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.freedesktop.systemd1." + interface, name)),
            )

        units: list[Unit] = []
        for name in UNIT_NAMES:
            try:
                try:
                    unit_path = call(
                        manager,
                        MANAGER_PATH,
                        BUS_NAME + ".Manager",
                        "GetUnit",
                        GLib.Variant("(s)", (name,)),
                    )
                except GLib.Error as lookup_error:
                    if Gio.DBusError.get_remote_error(lookup_error) != BUS_NAME + ".NoSuchUnit":
                        raise
                    # Inactive units can be garbage-collected between reads.
                    # Load only their definitions, never enqueue a start job.
                    # Already-loaded units retain their existing commands so
                    # a stale running manager is not disguised as updated.
                    unit_path = call(
                        manager,
                        MANAGER_PATH,
                        BUS_NAME + ".Manager",
                        "LoadUnit",
                        GLib.Variant("(s)", (name,)),
                    )
            except GLib.Error as error:
                if Gio.DBusError.get_remote_error(error) != BUS_NAME + ".NoSuchUnit":
                    raise
                try:
                    call(
                        manager,
                        MANAGER_PATH,
                        BUS_NAME + ".Manager",
                        "GetUnitFileState",
                        GLib.Variant("(s)", (name,)),
                    )
                except GLib.Error as file_error:
                    if Gio.DBusError.get_remote_error(file_error) not in (
                        BUS_NAME + ".NoSuchUnitFile",
                        "org.freedesktop.DBus.Error.FileNotFound",
                    ):
                        raise
                    units.append(Unit(name, "absent"))
                else:
                    units.append(Unit(name, "not-loaded"))
                continue
            if not isinstance(unit_path, str) or not unit_path.startswith(MANAGER_PATH + "/unit/"):
                raise ValueError("invalid service unit object path")
            load = prop(unit_path, "Unit", "LoadState")
            if load != "loaded":
                units.append(Unit(name, "absent" if load == "not-found" else "unverified"))
                continue
            fragment = prop(unit_path, "Unit", "FragmentPath")
            drop_ins = prop(unit_path, "Unit", "DropInPaths")
            reload = prop(unit_path, "Unit", "NeedDaemonReload")
            pid = prop(unit_path, "Service", "MainPID")
            if (
                not isinstance(fragment, str)
                or not isinstance(drop_ins, list)
                or not all(isinstance(item, str) for item in drop_ins)
                or type(reload) is not bool
                or type(pid) is not int
                or pid < 0
            ):
                raise ValueError("invalid loaded unit properties")
            commands = {
                key: _commands(prop(unit_path, "Service", key))
                for key in ("ExecStartPre", "ExecStart", "ExecStop", *EXTRA_COMMANDS)
            }
            environment = prop(unit_path, "Service", "Environment")
            files = prop(unit_path, "Service", "EnvironmentFiles")
            if not isinstance(environment, list) or not all(
                isinstance(item, str) for item in environment
            ):
                raise ValueError("invalid unit environment")
            if not isinstance(files, list) or not all(
                isinstance(item, tuple | list) and len(item) == 2 and isinstance(item[0], str)
                for item in files
            ):
                raise ValueError("invalid unit environment-file inventory")
            # Never include environment values or file contents in a report;
            # custom credentials are not useful update-diagnosis evidence.
            units.append(
                Unit(
                    name,
                    "loaded",
                    fragment,
                    tuple(drop_ins),
                    reload,
                    pid,
                    commands,
                    tuple(item.split("=", 1)[0] for item in environment),
                    tuple(item[0] for item in files),
                )
            )
        if owner() != manager:
            raise ValueError("the service manager changed during inspection")
        return tuple(units)
    finally:
        timer.cancel()
        if connection is not None:
            # Closing is asynchronous: cleanup cannot add an unbounded wait
            # after the inspection's deadline has already expired.
            connection.close(None, None, None)


def _matches_package(unit: Unit, root: Path) -> bool:
    if unit.environment_keys or unit.environment_files:
        # Custom import/config environments need their configuration owner's
        # inspection. Matching argv alone cannot establish the same profile.
        return False
    if any(unit.commands.get(name) != () for name in EXTRA_COMMANDS):
        return False
    app = str(root / "bin/wall-in-one")
    runtime = str(root / "bin/wall-in-one-service")
    if unit.name == UNIT_NAMES[1]:
        expected = {
            "ExecStartPre": (),
            "ExecStart": (Command(app, (app, "--sync-runtime-health")),),
            "ExecStop": (),
            **{name: () for name in EXTRA_COMMANDS},
        }
        return unit.commands == expected
    commands = unit.commands
    stop = commands.get("ExecStop", ())
    return (
        commands.get("ExecStartPre")
        == (
            Command(app, (app, "--service-startup-prepare")),
            Command(runtime, (runtime, "--check-config")),
        )
        and commands.get("ExecStart") == (Command(runtime, (runtime,)),)
        and len(stop) == 1
        and stop[0].ignore_errors
        and Path(stop[0].executable).name == "timeout"
        and stop[0].arguments
        == (
            stop[0].executable,
            "--signal=TERM",
            "--kill-after=0.1s",
            "2s",
            app,
            "--sync-runtime-health-on-stop",
        )
    )


def inspect_loaded_units(*, timeout: float = TIMEOUT) -> LoadedUnits:
    try:
        if timeout <= 0:
            raise ValueError("service inspection deadline expired")
        address = _session_address()
        if address is None:
            return LoadedUnits(
                "unmanaged", "No local user-service manager is available to inspect."
            )
        units = _read_units(address, timeout)
        if units is None:
            return LoadedUnits("unmanaged", "The session bus has no running user-service manager.")
    except Exception as error:
        return LoadedUnits("unverified", f"Cannot inspect loaded wallpaper services: {error}")
    if all(unit.state == "absent" for unit in units):
        return LoadedUnits(
            "absent", "No packaged wallpaper service units are installed or loaded.", units
        )
    root = package_root()
    if root is not None and all(
        unit.state == "absent"
        or (unit.state == "loaded" and not unit.needs_reload and _matches_package(unit, root))
        for unit in units
    ):
        return LoadedUnits(
            "current",
            "Loaded commands use this package; this is not a running-process acknowledgment.",
            units,
        )
    return LoadedUnits(
        "unverified",
        "Wallpaper service definitions are old, custom, unloaded, or need a daemon reload. "
        "Inspect them through the installation's configuration owner; no overrides were changed.",
        units,
    )


def report() -> dict[str, object]:
    loaded = inspect_loaded_units()
    runtime: dict[str, object]
    try:
        response = client.send_runtime("status", timeout=TIMEOUT)
        value = json.loads(response.message) if response.ok else None
        if not isinstance(value, dict):
            raise ValueError("runtime status is not an object")
        runtime = {"state": "responding", "status": value}
    except client.NotRunningError:
        runtime = {
            "state": "not-responding",
            "detail": "No status endpoint answered; this does not prove process absence.",
        }
    except (client.ControlError, ValueError, RecursionError) as error:
        runtime = {"state": "unverified", "detail": str(error)}
    root = package_root()
    return {
        "report_version": 1,
        "installed": {
            "version": __version__,
            "python_source": str(Path(__file__).resolve().parent),
            "package_root": str(root) if root is not None else None,
        },
        "loaded_services": asdict(loaded),
        "running_runtime": runtime,
        "settings_path": str(paths.settings_path()),
        "runtime_config_path": str(paths.runtime_config_path()),
        "read_only": True,
        "handover_ready": False,
    }
