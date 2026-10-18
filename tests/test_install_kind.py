"""Every build says what kind of install it is: nix, system or source.

The companion tells a Nix install from a distribution package by it (and two at
once from one), so the value has to be right in each package and the same in
both of its halves: this Python app, which a package sets with
``tools/set-install-kind.sh``, and the Rust service, which reads
``WALL_IN_ONE_INSTALL_KIND`` when cargo builds it. docs/packaging.md.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from wall_in_one import __version__, cli, install_kind, update_status
from wall_in_one.control import client

ROOT = Path(__file__).resolve().parents[1]
SET_SCRIPT = ROOT / "tools" / "set-install-kind.sh"
CHECK_SCRIPT = ROOT / "tools" / "check-install-kind.sh"
MODULE = Path(install_kind.__file__)


def test_this_build_is_one_of_the_three_kinds_and_the_one_its_build_says() -> None:
    """Unpackaged, ``source``; in a package, what the package built both halves as.

    A package's test run has ``WALL_IN_ONE_INSTALL_KIND`` set for the service's
    build, so this is also the check that the Python half was set to the same.
    """
    assert install_kind.INSTALL_KINDS == ("nix", "system", "source")
    assert install_kind.INSTALL_KIND in install_kind.INSTALL_KINDS
    built_as = os.environ.get("WALL_IN_ONE_INSTALL_KIND", "source")
    assert built_as == install_kind.INSTALL_KIND


def test_version_prints_the_release_then_the_install_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.splitlines() == [
        f"wall-in-one {__version__}",  # what every earlier release printed
        f"install kind: {install_kind.INSTALL_KIND}",
    ]


def test_the_update_report_names_the_installed_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_running(*_args: object, **_kwargs: object) -> object:
        raise client.NotRunningError("no runtime")

    monkeypatch.setattr(update_status, "_session_address", lambda: "unix:path=/isolated/bus")
    monkeypatch.setattr(update_status, "_read_units", lambda _address, _timeout: None)
    monkeypatch.setattr(client, "send_runtime", not_running)
    installed = update_status.report()["installed"]
    assert isinstance(installed, dict)
    assert installed["version"] == __version__
    assert installed["install_kind"] == install_kind.INSTALL_KIND


# -- tools/set-install-kind.sh: what a package runs before it builds ------------------


def _tree(tmp_path: Path) -> Path:
    """A copy of the two files the script touches, laid out as in the repository."""
    tree = tmp_path / "tree"
    (tree / "tools").mkdir(parents=True)
    (tree / "src" / "wall_in_one").mkdir(parents=True)
    shutil.copy2(SET_SCRIPT, tree / "tools" / SET_SCRIPT.name)
    (tree / "src" / "wall_in_one" / "install_kind.py").write_text(
        _checked_in(MODULE.read_text(encoding="utf-8")), encoding="utf-8"
    )
    return tree


def _checked_in(text: str) -> str:
    """The module as committed, whatever this run's package set it to."""
    lines = text.splitlines(keepends=True)
    marked = [i for i, line in enumerate(lines) if line.startswith("INSTALL_KIND: ")]
    assert len(marked) == 1, "one INSTALL_KIND line for the script to rewrite"
    lines[marked[0]] = 'INSTALL_KIND: Final[InstallKind] = "source"\n'
    return "".join(lines)


def _set(tree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "tools" / SET_SCRIPT.name), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_installed_module_has_the_line_the_script_rewrites() -> None:
    lines = MODULE.read_text(encoding="utf-8").splitlines()
    assert lines.count(f'INSTALL_KIND: Final[InstallKind] = "{install_kind.INSTALL_KIND}"') == 1


@pytest.mark.parametrize("kind", ["nix", "system", "source"])
def test_setting_a_kind_rewrites_that_one_line_and_nothing_else(tmp_path: Path, kind: str) -> None:
    tree = _tree(tmp_path)
    module = tree / "src" / "wall_in_one" / "install_kind.py"
    before = module.read_text(encoding="utf-8").splitlines()

    result = _set(tree, kind)
    assert result.returncode == 0, result.stderr
    after = module.read_text(encoding="utf-8").splitlines()
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    if kind == "source":
        assert changed == []
    else:
        assert changed == [
            (
                'INSTALL_KIND: Final[InstallKind] = "source"',
                f'INSTALL_KIND: Final[InstallKind] = "{kind}"',
            )
        ]
    assert runpy.run_path(str(module))["INSTALL_KIND"] == kind

    # Once set, setting it again (a rebuild in the same tree) still works.
    assert _set(tree, "system").returncode == 0
    assert runpy.run_path(str(module))["INSTALL_KIND"] == "system"


@pytest.mark.parametrize("arguments", [(), ("arch",), ("Nix",), ("",), ("nix", "system")])
def test_a_kind_it_does_not_know_changes_nothing(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    tree = _tree(tmp_path)
    module = tree / "src" / "wall_in_one" / "install_kind.py"
    before = module.read_bytes()
    result = _set(tree, *arguments)
    assert result.returncode == 2
    assert module.read_bytes() == before


def test_a_module_without_the_line_fails_the_build(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    module = tree / "src" / "wall_in_one" / "install_kind.py"
    module.write_text('INSTALL_KIND = "source"\n', encoding="utf-8")
    result = _set(tree, "nix")
    assert result.returncode == 1
    assert "no single INSTALL_KIND line" in result.stderr
    assert module.read_text(encoding="utf-8") == 'INSTALL_KIND = "source"\n'


# -- tools/check-install-kind.sh: both halves of one package agree ---------------------


def _program(directory: Path, name: str, *lines: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    body = "".join(f"printf '%s\\n' {line!r}\n" for line in lines)
    program.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    program.chmod(0o755)
    return program


def _check(kind: str, app: Path, service: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CHECK_SCRIPT), kind, str(app), str(service)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_two_halves_that_agree_pass(tmp_path: Path) -> None:
    app = _program(tmp_path, "wall-in-one", "wall-in-one 0.4.0", "install kind: system")
    service = _program(
        tmp_path, "wall-in-one-service", "wall-in-one-service 0.4.0", "install kind: system"
    )
    result = _check("system", app, service)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("app_kind", "service_kind"),
    [("system", "source"), ("source", "system"), ("nix", "nix")],
)
def test_two_halves_that_disagree_with_the_package_fail(
    tmp_path: Path, app_kind: str, service_kind: str
) -> None:
    app = _program(tmp_path, "wall-in-one", "wall-in-one 0.4.0", f"install kind: {app_kind}")
    service = _program(
        tmp_path,
        "wall-in-one-service",
        "wall-in-one-service 0.4.0",
        f"install kind: {service_kind}",
    )
    result = _check("system", app, service)
    assert result.returncode == 1
    assert "expected 'install kind: system'" in result.stderr


def test_a_release_that_prints_no_kind_fails(tmp_path: Path) -> None:
    """0.3's programs print one line: no kind is not a kind."""
    app = _program(tmp_path, "wall-in-one", "wall-in-one 0.3.2")
    service = _program(tmp_path, "wall-in-one-service", "wall-in-one-service 0.3.2")
    assert _check("source", app, service).returncode == 1
