"""``--write-config`` says exactly what it wrote, and compiles the same bytes from any terminal.

Two backlog findings from the 0.2.0 upgrade VM:

* it printed "wrote: runtime.toml" when only ``runtime-overrides.toml``
  changed (and nothing about the overrides file at all);
* ``runtime.toml``'s renderer programs came from the caller's ``PATH``
  (0.1.4's ``shutil.which``), so a run from a terminal compiled different
  bytes from the service's run and churned the file.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.library import playlists


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir(exist_ok=True)
    (root / "one.png").write_bytes(b"fixture")
    return root


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return program


def _write_config(capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert cli.main(["--write-config"]) == 0
    return capsys.readouterr().out.splitlines()


# -- what it says it wrote --------------------------------------------------------------------


def test_only_the_overrides_file_changing_is_said_as_that(
    library: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = config.Settings(roots=(library,), scan_workshop=False, cycle_interval=300)
    config.save(settings)
    store = playlists.Store.open()
    made = store.create("Evening")
    store.add(made.id, library / "one.png")
    target = paths.runtime_config_path()
    sidecar = runtime_config.overrides_path(target)

    assert _write_config(capsys) == [f"wrote: {target}"], "no overrides yet"
    main = target.read_bytes()

    store.set_rotation(made.id, cycle_interval=60)  # a per-playlist interval: overrides only
    assert _write_config(capsys) == [f"wrote: {sidecar}"]
    assert target.read_bytes() == main, "runtime.toml itself is untouched"

    assert _write_config(capsys) == [f"nothing changed: {target} is already current"]

    store.set_rotation(made.id, cycle_interval=None)  # back to the default: no overrides
    assert _write_config(capsys) == [f"removed: {sidecar}"]
    assert not sidecar.exists() and target.read_bytes() == main

    config.save(replace(settings, cycle_interval=600))
    store.set_rotation(made.id, cycle_interval=120)
    assert _write_config(capsys) == [f"wrote: {sidecar}", f"wrote: {target}"]


def test_update_still_answers_whether_anything_changed(library: Path) -> None:
    """The GUI's publication path keeps its yes/no answer."""
    from wall_in_one.session import Session

    settings = config.Settings(roots=(library,), scan_workshop=False)
    session = Session(settings)
    try:
        session.refresh(mutate_removals=False)
        assert runtime_config.update(settings, session) is True
        assert runtime_config.update(settings, session) is False
        assert not runtime_config.publish_changes(settings, session)
    finally:
        session.shutdown()


# -- the same bytes from any caller -----------------------------------------------------------


def test_a_terminal_and_the_service_compile_the_same_renderer_programs(
    tmp_path: Path,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The service's PATH is its user manager's; a terminal's has whatever else the person has.

    The package's own store directories and the fixed profile and system
    places come before the rest of PATH, so with the renderers in those
    standard places the terminal's extra directories cannot change a byte.
    """
    store = tmp_path / "store"
    monkeypatch.setattr(runtime_config, "NIX_STORE", store, raising=False)
    # The package wrapper's bundled renderers, at the front of every caller's PATH.
    bundled = store / "abc-renderers" / "bin"
    for name in ("mpvpaper", "linux-wallpaperengine"):
        _executable(bundled, name)
    # Noctalia is the person's, in their Nix profile.
    profile = _executable(Path.home() / ".nix-profile" / "bin", "noctalia")
    # A terminal's own extras: a checkout's build, a project's bin.
    terminal = tmp_path / "terminal-bin"
    for name in ("noctalia", "mpvpaper"):
        _executable(terminal, name)

    config.save(config.Settings(roots=(library,), scan_workshop=False))
    target = paths.runtime_config_path()

    monkeypatch.setenv("PATH", f"{bundled}{os.pathsep}/run/current-system/sw/bin")
    assert _write_config(capsys) == [f"wrote: {target}"], "the service's run"
    service = target.read_bytes()

    monkeypatch.setenv("PATH", os.pathsep.join((str(terminal), str(bundled), "/usr/bin")))
    assert _write_config(capsys) == [f"nothing changed: {target} is already current"]
    assert target.read_bytes() == service, "the terminal's run compiles the same bytes"

    renderer = service.decode()
    assert f'noctalia_program = "{profile.resolve()}"' in renderer
    for name, key in (
        ("mpvpaper", "mpvpaper_program"),
        ("linux-wallpaperengine", "linux_wallpaperengine_program"),
    ):
        assert f'{key} = "{(bundled / name).resolve()}"' in renderer
    assert str(terminal) not in renderer
    assert "niri_program" not in renderer


def test_the_search_order_is_store_profiles_system_then_the_rest_of_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    monkeypatch.setattr(runtime_config, "NIX_STORE", store)
    first, second = store / "a" / "bin", store / "b" / "bin"
    elsewhere, later = tmp_path / "elsewhere", tmp_path / "later"
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            (str(elsewhere), str(first), "", "relative/bin", "/usr/bin", str(second), str(later))
        ),
    )
    home = Path.home()
    order = runtime_config.program_directories()
    assert order[:2] == (first, second), "store directories, in PATH order"
    assert order[2:4] == (
        home / ".nix-profile" / "bin",
        home / ".local" / "state" / "nix" / "profile" / "bin",
    )
    assert Path("/run/current-system/sw/bin") in order
    assert order[-6:] == (
        home / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        elsewhere,
        later,
    ), "then the rest of PATH, in PATH order, each place once"
    assert order.index(Path("/run/current-system/sw/bin")) < order.index(home / ".local" / "bin")
    assert Path("relative/bin") not in order, "a relative entry would depend on the caller's cwd"


def test_a_renderer_only_in_an_unusual_place_on_path_is_still_found(
    tmp_path: Path,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An install in /opt, ~/bin or a custom prefix keeps working after the update."""
    monkeypatch.setattr(runtime_config, "NIX_STORE", tmp_path / "store")
    custom = tmp_path / "opt" / "renderers" / "bin"
    monkeypatch.setenv("PATH", os.pathsep.join((str(custom), "/nonexistent/bin")))
    standard = [d for d in runtime_config.program_directories() if d != custom]

    # The lookup itself, for a name no machine has anywhere standard.
    only_here = _executable(custom, "wio-only-in-a-custom-prefix")
    assert runtime_config._program(only_here.name) == only_here.resolve()

    # And in a compiled runtime.toml, for each renderer this machine doesn't
    # already have in a standard place (in a build sandbox: all four).
    keys = {
        "noctalia": "noctalia_program",
        "mpvpaper": "mpvpaper_program",
        "linux-wallpaperengine": "linux_wallpaperengine_program",
    }
    missing = {
        name: key
        for name, key in keys.items()
        if not any((directory / name).exists() for directory in standard)
    }
    installed = {name: _executable(custom, name) for name in missing}
    config.save(config.Settings(roots=(library,), scan_workshop=False))
    _write_config(capsys)
    renderer = paths.runtime_config_path().read_text(encoding="utf-8")
    for name, key in missing.items():
        assert f'{key} = "{installed[name].resolve()}"' in renderer
