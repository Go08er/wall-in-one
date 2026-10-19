"""What Wall-in-One finds missing beside itself (docs/missing-components.md).

The checker's pure half is driven with observations built by hand; its
observing half with a fake home, fake Noctalia folders, fake units and a
fake program lookup, never this machine's. ``--setup-status`` prints the
same findings and exits 1 while something required is missing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wall_in_one import cli, programs, setup_check
from wall_in_one.install_kind import INSTALL_KINDS, InstallKind
from wall_in_one.setup_check import Companion, Local, MissingRenderer, Service

SRC = Path(__file__).resolve().parents[1] / "src" / "wall_in_one"


def _program(name: str, found: bool, where: str = "/usr/bin") -> programs.Program:
    return programs.Program(name, Path(where) / name, found=found)


INSTALLED = Companion("installed", "/somewhere/wall-in-one")


def _local(
    *,
    missing: tuple[str, ...] = (),
    unit: bool | None = True,
    companion: Companion = INSTALLED,
) -> Local:
    return Local(
        tuple(_program(name, name not in missing) for name in setup_check.PROGRAMS),
        unit,
        companion,
    )


RUNNING = Service("running")


def _keys(found: tuple[setup_check.Finding, ...]) -> list[tuple[str, bool]]:
    return [(finding.key, finding.required) for finding in found]


# -- the pure half ----------------------------------------------------------------


def test_nothing_is_said_when_everything_is_there() -> None:
    found = setup_check.findings("nix", _local(), RUNNING)
    assert found == ()
    assert setup_check.required(found) == ()
    assert setup_check.report(found) == f"ok: {setup_check.ALL_PRESENT}"


def test_no_renderer_at_all_is_one_required_finding_with_each_install_kinds_fix() -> None:
    fixes = {}
    for kind in INSTALL_KINDS:
        found = setup_check.findings(kind, _local(missing=setup_check.RENDERERS), RUNNING)
        assert _keys(found) == [("renderers", True)], kind
        fixes[kind] = found[0].fix
    assert "programs.wall-in-one" in fixes["nix"]
    assert "manualWallpaperInstalls" in fixes["nix"]
    assert "nix profile install nixpkgs#mpvpaper" in fixes["nix"]
    assert "nixpkgs#linux-wallpaperengine" in fixes["nix"]
    assert "Install mpvpaper from the AUR." in fixes["system"]
    assert "Install linux-wallpaperengine-git from the AUR." in fixes["system"]
    assert "nix" not in fixes["system"] and "AUR" not in fixes["source"]
    assert "/usr/local/bin" in fixes["source"]


@pytest.mark.parametrize("name", setup_check.RENDERERS)
def test_one_renderer_missing_is_optional(name: str) -> None:
    found = setup_check.findings("system", _local(missing=(name,)), RUNNING)
    assert _keys(found) == [(f"renderer:{name}", False)]
    assert found[0].title == f"{name} isn't installed"
    assert "show their still" in found[0].detail
    assert setup_check.required(found) == ()


def test_a_renderer_the_service_could_not_start_says_check_again() -> None:
    moved = Service("running", (MissingRenderer("mpvpaper", "/usr/bin/mpvpaper"),))
    local = Local(
        (
            _program("mpvpaper", True, "/home/me/.nix-profile/bin"),
            _program("linux-wallpaperengine", True),
            _program("noctalia", True),
            _program("wall-in-one-service", True),
        ),
        True,
        Companion("installed"),
    )
    (finding,) = setup_check.findings("nix", local, moved)
    assert (finding.key, finding.required) == ("renderer:mpvpaper", False)
    assert "/usr/bin/mpvpaper" in finding.detail
    assert "/home/me/.nix-profile/bin/mpvpaper" in finding.detail
    assert "Check Again" in finding.fix
    assert "wall-in-one --write-config" in finding.fix

    # The same path: it is there now, and the service has not tried since.
    same = setup_check.findings(
        "system",
        _local(),
        Service(
            "running", (MissingRenderer("linux-wallpaperengine", "/usr/bin/linux-wallpaperengine"),)
        ),
    )
    assert _keys(same) == [("renderer:linux-wallpaperengine", False)]
    assert "when it last tried" in same[0].detail


def test_noctalia_missing_is_required() -> None:
    fixes = {}
    for kind in INSTALL_KINDS:
        found = setup_check.findings(kind, _local(missing=("noctalia",)), RUNNING)
        assert _keys(found) == [("noctalia", True)], kind
        fixes[kind] = found[0].fix
    assert "github:noctalia-dev/noctalia" in fixes["nix"]
    assert "the noctalia package" in fixes["system"]


def test_a_service_that_does_not_run_says_what_would_start_it() -> None:
    stopped = Service("not-running")
    with_unit = setup_check.findings("nix", _local(unit=True), stopped)
    assert _keys(with_unit) == [("service", True)]
    assert with_unit[0].title == "The wallpaper service isn't running"
    assert "systemctl --user enable --now wall-in-one.service" in with_unit[0].fix
    assert with_unit[0].link == setup_check.COMPANION_URL

    without_unit = setup_check.findings("source", _local(unit=False), stopped)
    assert _keys(without_unit) == [("service", True)]
    assert "no user unit" in without_unit[0].detail
    assert "companion plugin" in without_unit[0].fix
    assert setup_check.COMPANION_INSTALL in without_unit[0].fix
    assert without_unit[0].link == setup_check.COMPANION_URL

    unchecked = setup_check.findings("nix", _local(unit=None), stopped)
    assert "couldn't check whether its user unit" in unchecked[0].detail


@pytest.mark.parametrize(
    ("kind", "fix"),
    [
        ("nix", "nix profile install github:Go08er/wall-in-one"),
        ("system", "Reinstall the wall-in-one package"),
        ("source", "cargo install --path service"),
    ],
)
def test_a_service_that_is_not_installed_says_how_to_install_it(
    kind: InstallKind, fix: str
) -> None:
    found = setup_check.findings(
        kind, _local(missing=("wall-in-one-service",), unit=False), Service("not-running")
    )
    assert _keys(found) == [("service", True)]
    assert found[0].title == "The wallpaper service isn't installed"
    assert fix in found[0].fix


def test_a_service_that_does_not_answer_is_required_and_unknown_is_nothing() -> None:
    late = setup_check.findings("nix", _local(), Service("not-answering", detail="timed out"))
    assert _keys(late) == [("service", True)]
    assert "timed out" in late[0].detail
    # The window before its first status: nothing to say about the service.
    assert setup_check.findings("nix", _local(), Service("unknown")) == ()


def test_the_companion_is_optional_and_unknown_is_not_absent() -> None:
    absent = setup_check.findings("nix", _local(companion=Companion("absent")), RUNNING)
    assert _keys(absent) == [("companion", False)]
    assert absent[0].title == "The companion plugin wasn't found"
    assert "path source" in absent[0].detail
    assert setup_check.COMPANION_INSTALL in absent[0].fix
    assert absent[0].link == setup_check.COMPANION_URL

    unknown = setup_check.findings(
        "nix", _local(companion=Companion("unknown", "/state/noctalia/plugins")), RUNNING
    )
    assert _keys(unknown) == [("companion", False)]
    assert "couldn't tell" in unknown[0].title
    assert "/state/noctalia/plugins" in unknown[0].detail


def test_findings_come_most_important_first_and_change_when_their_text_does() -> None:
    found = setup_check.findings(
        "system",
        _local(missing=("noctalia", "mpvpaper"), companion=Companion("absent")),
        Service("not-running"),
    )
    assert _keys(found) == [
        ("service", True),
        ("noctalia", True),
        ("renderer:mpvpaper", False),
        ("companion", False),
    ]
    assert [finding.key for finding in setup_check.required(found)] == ["service", "noctalia"]
    later = setup_check.findings(
        "system",
        _local(
            missing=("noctalia", "mpvpaper", "wall-in-one-service"), companion=Companion("absent")
        ),
        Service("not-running"),
    )
    assert found[0].key == later[0].key
    assert found[0].fingerprint != later[0].fingerprint, "not running became not installed"
    assert found[1].fingerprint == later[1].fingerprint


def test_the_report_lists_required_first_with_each_fix() -> None:
    found = setup_check.findings(
        "nix", _local(missing=("mpvpaper",), unit=False), Service("not-running")
    )
    text = setup_check.report(reversed(found))
    lines = text.splitlines()
    assert lines[0] == "required: The wallpaper service isn't running."
    assert any(line.startswith("  fix: Install the companion plugin") for line in lines)
    assert f"  link: {setup_check.COMPANION_URL}" in lines
    assert "optional: mpvpaper isn't installed." in lines
    assert lines[-1] == setup_check.AFTER_INSTALLING


def test_the_companion_link_is_written_in_one_place() -> None:
    holders = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if setup_check.COMPANION_URL in path.read_text(encoding="utf-8")
    ]
    assert holders == ["setup_check.py"]


# -- what a status reply says --------------------------------------------------------


def test_a_status_without_missing_renderers_reports_none() -> None:
    """A 0.3 service has no such key."""
    assert setup_check.service_from_status({"status_version": 2}) == RUNNING
    assert setup_check.service_from_status({"missing_renderers": []}) == RUNNING


def test_a_status_lists_what_the_service_could_not_start_and_skips_the_rest() -> None:
    status = {
        "missing_renderers": [
            {"program": "mpvpaper", "path": "/usr/bin/mpvpaper"},
            {"program": "linux-wallpaperengine", "path": 3},
            {"program": "something-else", "path": "/x"},
        ]
    }
    assert setup_check.service_from_status(status) == Service(
        "running", (MissingRenderer("mpvpaper", "/usr/bin/mpvpaper"),)
    )
    assert setup_check.service_from_status({"missing_renderers": "nope"}) == RUNNING


# -- the observing half -------------------------------------------------------------


def _environment(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def _unit(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    unit = directory / "wall-in-one.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/wall-in-one-service\n")
    return unit


def test_the_user_unit_search_path_follows_systemd(tmp_path: Path) -> None:
    home = tmp_path / "home"
    environ = _environment(
        home,
        XDG_RUNTIME_DIR=str(tmp_path / "run"),
        XDG_DATA_DIRS=f"{tmp_path / 'profile' / 'share'}{os.pathsep}relative{os.pathsep}/usr/share",
    )
    directories = setup_check.user_unit_directories(environ)
    assert directories[0] == home / ".config" / "systemd" / "user"
    assert Path("/etc/systemd/user") in directories
    assert tmp_path / "run" / "systemd" / "user" in directories
    assert home / ".local" / "share" / "systemd" / "user" in directories
    assert tmp_path / "profile" / "share" / "systemd" / "user" in directories
    assert Path("/usr/lib/systemd/user") in directories
    assert all(directory.is_absolute() for directory in directories)
    assert not any("relative" in str(directory) for directory in directories)
    # No home: only the system places.
    bare = setup_check.user_unit_directories({"HOME": ""})
    assert not any(str(directory).startswith(str(home)) for directory in bare)


def test_the_unit_is_found_in_a_profile_and_in_the_users_own_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = tmp_path / "profile" / "share"
    environ = _environment(home, XDG_DATA_DIRS=str(profile), XDG_CONFIG_DIRS=str(tmp_path / "xdg"))
    if not any((directory / "wall-in-one.service").exists() for directory in _system_unit_dirs()):
        assert setup_check.unit_installed(environ) is False
    _unit(profile / "systemd" / "user")
    assert setup_check.unit_installed(environ) is True
    (profile / "systemd" / "user" / "wall-in-one.service").unlink()
    _unit(home / ".local" / "share" / "systemd" / "user")
    assert setup_check.unit_installed(environ) is True


def test_a_unit_folder_that_cannot_be_looked_in_is_not_proof_of_absence(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads every folder")
    if any((directory / "wall-in-one.service").exists() for directory in _system_unit_dirs()):
        pytest.skip("this machine has the unit installed system-wide")
    home = tmp_path / "home"
    locked = home / ".config" / "systemd" / "user"
    locked.mkdir(parents=True)
    environ = _environment(
        home, XDG_DATA_DIRS=str(tmp_path / "none"), XDG_CONFIG_DIRS=str(tmp_path / "none")
    )
    assert setup_check.unit_installed(environ) is False
    locked.chmod(0)
    try:
        assert setup_check.unit_installed(environ) is None
    finally:
        locked.chmod(0o700)


def _system_unit_dirs() -> tuple[Path, ...]:
    return (
        Path("/etc/systemd/user"),
        Path("/run/systemd/user"),
        Path("/usr/local/lib/systemd/user"),
        Path("/usr/lib/systemd/user"),
    )


def _manifest(folder: Path, plugin_id: str = setup_check.COMPANION_ID) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.toml").write_text(
        f'id = "{plugin_id}"\nname = "Wall-in-One"\nversion = "0.2.1"\n'
    )
    return folder


def test_the_companion_exported_by_a_git_source_is_found(tmp_path: Path) -> None:
    home = tmp_path / "home"
    environ = _environment(home)
    assert setup_check.companion(environ) == Companion("absent")
    # Another plugin from the same source, and a folder that is not a plugin.
    materialized = home / ".local" / "state" / "noctalia" / "plugins" / "materialized" / "goober-v5"
    _manifest(materialized / "hydra-update-examiner", "goober/hydra-update-examiner")
    (materialized / "scratch").mkdir()
    assert setup_check.companion(environ) == Companion("absent")
    # Its id, not its folder's name, is what counts.
    folder = _manifest(materialized / "renamed")
    assert setup_check.companion(environ) == Companion("installed", str(folder))


def test_noctalias_own_overrides_win(tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = tmp_path / "noctalia-state"
    data = tmp_path / "noctalia-data"
    _manifest(
        home / ".local" / "state" / "noctalia" / "plugins" / "materialized" / "x" / "wall-in-one"
    )
    environ = _environment(
        home,
        NOCTALIA_STATE_HOME=str(state),
        NOCTALIA_DATA_HOME=str(data),
        XDG_STATE_HOME=str(tmp_path / "xdg-state"),
    )
    assert setup_check.companion(environ) == Companion("absent"), "the XDG one is not Noctalia's"
    local = _manifest(data / "noctalia" / "plugins" / "wall-in-one")
    assert setup_check.companion(environ) == Companion("installed", str(local))
    # The XDG variable comes next, before the default under HOME.
    xdg = _environment(home, XDG_STATE_HOME=str(tmp_path / "xdg-state"))
    exported = _manifest(
        tmp_path / "xdg-state" / "noctalia" / "plugins" / "materialized" / "s" / "w"
    )
    assert setup_check.companion(xdg) == Companion("installed", str(exported))


def test_the_companion_is_unknown_when_it_cannot_be_told(tmp_path: Path) -> None:
    assert setup_check.companion({}).state == "unknown", "no home at all"
    assert setup_check.companion({"HOME": "relative"}).state == "unknown"
    home = tmp_path / "home"
    assert setup_check.companion(_environment(home, NOCTALIA_STATE_HOME="relative")).state == (
        "unknown"
    )
    # A manifest too large to be one is not a plugin.
    big = home / ".local" / "share" / "noctalia" / "plugins" / "big"
    big.mkdir(parents=True)
    (big / "plugin.toml").write_text(
        f'id = "{setup_check.COMPANION_ID}"\n# {"x" * setup_check.MAX_MANIFEST_BYTES}\n'
    )
    assert setup_check.companion(_environment(home)) == Companion("absent")
    if os.geteuid() == 0:
        return
    materialized = home / ".local" / "state" / "noctalia" / "plugins" / "materialized"
    materialized.mkdir(parents=True)
    materialized.chmod(0)
    try:
        found = setup_check.companion(_environment(home))
    finally:
        materialized.chmod(0o700)
    assert found == Companion("unknown", str(materialized))


def test_local_looks_through_the_program_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ("mpvpaper", "noctalia"):
        program = bin_directory / name
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    monkeypatch.setattr(programs, "program_directories", lambda: (bin_directory,))
    found = setup_check.local(_environment(tmp_path / "home"))
    assert found.program("mpvpaper") == programs.Program(
        "mpvpaper", bin_directory / "mpvpaper", True
    )
    assert not found.program("linux-wallpaperengine").found
    assert not found.program("wall-in-one-service").found
    assert found.companion == Companion("absent")


# -- --setup-status ----------------------------------------------------------------------


def test_setup_status_prints_the_findings_and_fails_while_something_required_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No service runs in a test's own runtime folder, and nothing is installed."""
    monkeypatch.setattr(programs, "program_directories", lambda: (tmp_path / "bin",))
    assert cli.main(["--setup-status"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("required: The wallpaper service isn't installed.\n")
    assert "required: Noctalia isn't installed." in out
    assert "required: No wallpaper renderer is installed." in out
    assert setup_check.AFTER_INSTALLING in out


def test_setup_status_succeeds_with_only_optional_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ("mpvpaper", "noctalia", "wall-in-one-service"):
        program = bin_directory / name
        program.write_text("#!/bin/sh\n")
        program.chmod(0o755)
    monkeypatch.setattr(programs, "program_directories", lambda: (bin_directory,))
    asked: list[float] = []

    def running(timeout: float) -> Service:
        asked.append(timeout)
        return RUNNING

    monkeypatch.setattr(setup_check, "ask_service", running)
    assert cli.main(["--setup-status"]) == 0
    out = capsys.readouterr().out
    assert "optional: linux-wallpaperengine isn't installed." in out
    assert "required:" not in out
    assert asked, "the service was asked"


def test_setup_status_is_in_help() -> None:
    assert "--setup-status" in cli._build_parser().format_help()
