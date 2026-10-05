"""The upgrade-rollback VM stops a launch unit only once the kernel says it is empty.

Codex 0.2.2 N-1: the census read ``cgroup.procs`` and then ran ``ps`` on each
PID. A listed process that forked and exited in between left a child the list
never named, the census came back empty, and the unit's stop could signal
that child. The helpers are the VM script's own, taken from
``nix/upgrade-rollback/test_script.py`` by name and run against a scripted
guest; only names the script defines are taken, so an older script is
judged by what it actually does.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "nix" / "upgrade-rollback" / "test_script.py"
HELPERS = ("unit_processes", "unit_populated", "wait_unit_drained")
CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/launch.service"


class Clock:
    """``time`` for the helpers: sleeping advances it, nothing waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Guest:
    """A launch unit whose one listed process forks a helper and exits as the list is read.

    ``helper_exits_after`` is how many polls the helper lives through; None
    keeps it running.
    """

    def __init__(self, helper_exits_after: int | None) -> None:
        self.members = ["4242"]
        self.live = {"4242"}
        self.helper_exits_after = helper_exits_after
        self.polls = 0
        self.populated_when_stopped: list[bool] = []

    def succeed(self, command: str) -> str:
        if command == f"cat /sys/fs/cgroup{CGROUP}/cgroup.procs":
            listed = " ".join(self.members)
            if "4242" in self.live:
                # The race: after the list is read, 4242 forks 4343 and exits.
                self.live = {"4343"}
                self.members = ["4343"]
            return listed + "\n"
        if command == f"cat /sys/fs/cgroup{CGROUP}/cgroup.events":
            self.polls += 1
            if self.helper_exits_after is not None and self.polls > self.helper_exits_after:
                self.live = set()
                self.members = []
            return f"populated {1 if self.live else 0}\nfrozen 0\n"
        raise AssertionError(f"unexpected guest command: {command}")

    def execute(self, command: str) -> tuple[int, str]:
        pid = command.rsplit(" ", 1)[1]
        if command.startswith("ps ") and pid in self.live:
            return 0, f"{pid} {pid} S sh -c sleep 6; touch /tmp/done"
        return 1, ""


def _helpers(guest: Guest, clock: Clock, stops: list[str]) -> dict[str, Any]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    taken: list[ast.stmt] = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    names = {node.name for node in taken if isinstance(node, ast.FunctionDef)}
    assert names >= {"unit_processes", "wait_unit_drained"}

    def run(command: str) -> str:
        stops.append(command)
        guest.populated_when_stopped.append(bool(guest.live))
        return ""

    namespace: dict[str, Any] = {
        "machine": guest,
        "time": clock,
        "prop": lambda name, unit: CGROUP if name == "ControlGroup" else "",
        "run": run,
        "observe": lambda _label, _value: None,
    }
    exec(compile(ast.Module(body=taken, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace


def test_a_child_forked_after_the_census_keeps_the_unit_from_being_stopped() -> None:
    guest, clock = Guest(helper_exits_after=None), Clock()
    stops: list[str] = []
    helpers = _helpers(guest, clock, stops)
    with pytest.raises(AssertionError) as failure:
        helpers["wait_unit_drained"]("launch.service", timeout=5)
    assert stops == [], "a unit with a live helper was stopped"
    assert "4343" in str(failure.value), "the survivor is named"


def test_the_unit_is_stopped_once_its_cgroup_is_empty() -> None:
    guest, clock = Guest(helper_exits_after=3), Clock()
    stops: list[str] = []
    helpers = _helpers(guest, clock, stops)
    helpers["wait_unit_drained"]("launch.service", timeout=5)
    assert stops == ["systemctl --user stop launch.service"]
    assert guest.populated_when_stopped == [False], "stopped only when nothing was left"
