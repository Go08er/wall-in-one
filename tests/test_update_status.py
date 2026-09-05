"""Loaded commands are separate evidence from a responding runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wall_in_one import cli, config, update_status
from wall_in_one.control import client
from wall_in_one.control.protocol import Response


def current_units(root: Path) -> tuple[update_status.Unit, ...]:
    app, runtime = (str(root / "bin" / name) for name in ("wall-in-one", "wall-in-one-service"))
    command = update_status.Command
    return (
        update_status.Unit(
            "wall-in-one.service",
            "loaded",
            commands={
                "ExecStartPre": (
                    command(app, (app, "--service-startup-prepare")),
                    command(runtime, (runtime, "--check-config")),
                ),
                "ExecStart": (command(runtime, (runtime,)),),
                "ExecStop": (
                    command(
                        "/tools/timeout",
                        (
                            "/tools/timeout",
                            "--signal=TERM",
                            "--kill-after=0.1s",
                            "2s",
                            app,
                            "--sync-runtime-health-on-stop",
                        ),
                        True,
                    ),
                ),
                **{name: () for name in update_status.EXTRA_COMMANDS},
            },
        ),
        update_status.Unit(
            "wall-in-one-health-sync.service",
            "loaded",
            commands={
                "ExecStartPre": (),
                "ExecStart": (command(app, (app, "--sync-runtime-health")),),
                "ExecStop": (),
                **{name: () for name in update_status.EXTRA_COMMANDS},
            },
        ),
    )


def mock_units(
    monkeypatch: pytest.MonkeyPatch, root: Path, units: tuple[update_status.Unit, ...] | None
) -> None:
    monkeypatch.setattr(update_status, "package_root", lambda: root)
    monkeypatch.setattr(update_status, "_session_address", lambda: "unix:path=/isolated/bus")
    monkeypatch.setattr(update_status, "_read_units", lambda _address, _timeout: units)


@pytest.mark.parametrize(
    "field",
    [
        "ExecStartPre",
        "ExecStart",
        "ExecStop",
        "health",
        "needs_reload",
        "not-loaded",
        "unknown-source",
        "environment",
        "environment-file",
        "ignore-preflight-error",
        "extra-post-start",
        "unsupported-startup-argument",
    ],
)
def test_new_runtime_cannot_hide_an_old_or_unverified_next_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    root = tmp_path / "new"
    current = current_units(root)
    old = current_units(tmp_path / "old")
    main, health = current
    if field == "health":
        health = old[1]
    elif field == "needs_reload":
        main = replace(main, needs_reload=True)
    elif field == "not-loaded":
        main = replace(main, state="not-loaded")
    elif field == "environment":
        main = replace(main, environment_keys=("PYTHONPATH",))
    elif field == "environment-file":
        main = replace(main, environment_files=("/private/custom-environment",))
    elif field == "ignore-preflight-error":
        first, second = main.commands["ExecStartPre"]
        main = replace(
            main,
            commands={
                **main.commands,
                "ExecStartPre": (replace(first, ignore_errors=True), second),
            },
        )
    elif field == "extra-post-start":
        main = replace(
            main, commands={**main.commands, "ExecStartPost": old[1].commands["ExecStart"]}
        )
    elif field == "unsupported-startup-argument":
        command = main.commands["ExecStart"][0]
        main = replace(
            main,
            commands={
                **main.commands,
                "ExecStart": (
                    replace(command, arguments=(command.executable, "--obsolete-update-mode")),
                ),
            },
        )
    elif field != "unknown-source":
        main = replace(main, commands={**main.commands, field: old[0].commands[field]})
    mock_units(monkeypatch, root, (main, health))
    if field == "unknown-source":
        monkeypatch.setattr(update_status, "package_root", lambda: None)
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response.success(
            '{"status_version":2,"supported_config_schemas":[4,5]}'
        ),
    )
    path = config.save(config.Settings())
    before = path.read_bytes()
    with pytest.raises(config.ConfigError, match="next wallpaper service start"):
        config.update({"stop_animations_on_battery": True})
    assert path.read_bytes() == before
    assert update_status.inspect_loaded_units().state == "unverified"


def test_current_loaded_package_allows_capable_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mock_units(monkeypatch, tmp_path, current_units(tmp_path))
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response.success(
            '{"status_version":2,"supported_config_schemas":[4,5]}'
        ),
    )
    assert config.update({"stop_animations_on_battery": True}).stop_animations_on_battery
    assert update_status.inspect_loaded_units().state == "current"


def test_schema_four_does_not_require_update_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        update_status,
        "inspect_loaded_units",
        lambda **_kwargs: pytest.fail("queried additive feature gate"),
    )
    config.save(config.Settings())


@pytest.mark.parametrize("state", ["absent", "not-loaded", "unverified"])
def test_absent_is_not_the_same_as_unloaded_or_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    mock_units(
        monkeypatch,
        tmp_path,
        tuple(update_status.Unit(name, state) for name in update_status.UNIT_NAMES),
    )
    assert update_status.inspect_loaded_units().state == (
        "absent" if state == "absent" else "unverified"
    )


def test_inspection_error_is_not_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(update_status, "_session_address", broken)
    assert update_status.inspect_loaded_units().state == "unverified"
    assert update_status.inspect_loaded_units(timeout=0).state == "unverified"


def test_cli_report_does_not_enter_migration_or_graphical_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        update_status, "report", lambda: {"read_only": True, "handover_ready": False}
    )
    monkeypatch.setattr(
        cli, "_run_legacy_migration", lambda *_args: pytest.fail("entered a writer boundary")
    )
    assert cli.main(["--update-status"]) == 0
    assert '"handover_ready": false' in capsys.readouterr().out


@pytest.mark.parametrize("initially_loaded", [False, True])
@pytest.mark.parametrize("manager_changed", [False, True])
def test_structured_bus_reader_inspects_without_starting_or_reloading_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initially_loaded: bool, manager_changed: bool
) -> None:
    pytest.importorskip("gi")
    from gi.repository import Gio

    units = current_units(tmp_path)
    events: list[str] = []

    class Connection:
        def set_exit_on_close(self, value: bool) -> None:
            assert not value

        def call_sync(
            self,
            destination: str,
            path: str,
            interface: str,
            method: str,
            args: Any,
            _reply: object,
            flags: object,
            timeout: int,
            _cancel: object,
        ) -> Any:
            assert flags == Gio.DBusCallFlags.NO_AUTO_START
            assert 0 < timeout <= 5000
            events.append(method)
            if method == "GetNameOwner":
                value: object = ":1.8" if manager_changed and events.count(method) > 1 else ":1.7"
            elif method in ("GetUnit", "LoadUnit"):
                assert destination == ":1.7"
                if method == "GetUnit" and not initially_loaded:
                    raise Gio.DBusError.new_for_dbus_error(
                        update_status.BUS_NAME + ".NoSuchUnit", "not currently loaded"
                    )
                value = (
                    update_status.MANAGER_PATH
                    + "/unit/"
                    + str(update_status.UNIT_NAMES.index(args.unpack()[0]))
                )
            elif method == "Get":
                unit = units[int(path.rsplit("/", 1)[1])]
                key = args.unpack()[1]
                properties: dict[str, object] = {
                    "LoadState": "loaded",
                    "FragmentPath": "/unit",
                    "DropInPaths": [],
                    "NeedDaemonReload": False,
                    "MainPID": 42,
                    "Environment": [],
                    "EnvironmentFiles": [],
                }
                properties.update(
                    {
                        name: [
                            (
                                command.executable,
                                list(command.arguments),
                                command.ignore_errors,
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                            )
                            for command in commands
                        ]
                        for name, commands in unit.commands.items()
                    }
                )
                value = properties[key]
            else:
                pytest.fail(f"unexpected or mutating bus method: {method}")
            return SimpleNamespace(unpack=lambda: (value,))

        def close(self, *_args: object) -> None:
            events.append("closed")

    monkeypatch.setattr(Gio.DBusConnection, "new_for_address_sync", lambda *_args: Connection())
    if manager_changed:
        with pytest.raises(ValueError, match="manager changed"):
            update_status._read_units("unix:path=/never-contacted", 5.0)
    else:
        found = update_status._read_units("unix:path=/never-contacted", 5.0)
        assert found is not None and len(found) == 2
        assert found[0].commands == units[0].commands
    assert events[-1] == "closed"
    assert events.count("GetNameOwner") == 2
    assert events.count("LoadUnit") == (0 if initially_loaded else 2)


@pytest.mark.parametrize(
    "value", [None, "text", [()], [["bin", "not argv", *([0] * 8)]], [tuple(range(11))]]
)
def test_malformed_loaded_commands_fail_closed(value: object) -> None:
    with pytest.raises(ValueError):
        update_status._commands(value)
