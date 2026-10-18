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
import shutil
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

RENDERER_KEYS = {
    "noctalia": "noctalia_program",
    "mpvpaper": "mpvpaper_program",
    "linux-wallpaperengine": "linux_wallpaperengine_program",
}


def _generation(store: Path, number: int, *names: str) -> Path:
    """A fake profile generation: a ``bin`` of links into one store path per program."""
    generation = store / f"{number:03d}-profile" / "bin"
    generation.mkdir(parents=True)
    for name in names:
        package = _executable(store / f"{number:03d}-{name}" / "bin", name)
        (generation / name).symlink_to(package)
    return generation.parent


def _profile(generation: Path) -> Path:
    """``~/.nix-profile``, a link to ``generation``."""
    profile = Path.home() / ".nix-profile"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.symlink_to(generation)
    return profile


def _switch(profile: Path, generation: Path) -> None:
    """What a profile switch does: re-point the profile's link at another generation."""
    staged = profile.with_name(profile.name + ".new")
    staged.symlink_to(generation)
    staged.replace(profile)


def _renderer_lines(document: bytes) -> dict[str, str]:
    lines = document.decode().splitlines()
    return {
        key: line.split(" = ", 1)[1]
        for line in lines
        for key in RENDERER_KEYS.values()
        if line.startswith(f"{key} = ")
    }


def test_a_terminal_and_the_service_compile_the_same_renderer_programs(
    tmp_path: Path,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The service's PATH is its user manager's; a terminal's has whatever else the person has.

    The renderers are installed in the person's profile (the home-manager
    module's ``home.packages``, say). The profiles and the fixed system places
    come before the rest of PATH, and a store directory on PATH (a ``nix
    shell``) comes last, so the terminal's extra directories cannot change a
    byte.
    """
    store = tmp_path / "store"
    monkeypatch.setattr(runtime_config, "NIX_STORE", store, raising=False)
    profile = _profile(_generation(store, 1, *RENDERER_KEYS))
    # A terminal's own extras: a checkout's build, and a `nix shell` with a renderer.
    terminal = tmp_path / "terminal-bin"
    for name in ("noctalia", "mpvpaper"):
        _executable(terminal, name)
    shell = store / "zzz-mpvpaper" / "bin"
    _executable(shell, "mpvpaper")
    _executable(shell, "linux-wallpaperengine")

    config.save(config.Settings(roots=(library,), scan_workshop=False))
    target = paths.runtime_config_path()

    monkeypatch.setenv("PATH", "/run/current-system/sw/bin")
    assert _write_config(capsys) == [f"wrote: {target}"], "the service's run"
    service = target.read_bytes()

    monkeypatch.setenv("PATH", os.pathsep.join((str(shell), str(terminal), "/usr/bin")))
    assert _write_config(capsys) == [f"nothing changed: {target} is already current"]
    assert target.read_bytes() == service, "the terminal's run compiles the same bytes"

    assert _renderer_lines(service) == {
        key: f'"{profile / "bin" / name}"' for name, key in RENDERER_KEYS.items()
    }, "the profile's own path, not the store path behind it"
    assert str(terminal) not in service.decode()
    assert str(store) not in service.decode()
    assert "niri_program" not in service.decode()


def test_a_profile_switch_compiles_the_same_bytes(
    tmp_path: Path,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An update re-points the profile; the document names the profile, so it stays put.

    Until 0.4.0 the path was resolved into the store, so every renderer
    update rewrote runtime.toml, and a document written before the switch
    named a store path the next garbage collection deletes.
    """
    store = tmp_path / "store"
    monkeypatch.setattr(runtime_config, "NIX_STORE", store, raising=False)
    monkeypatch.setenv("PATH", "/run/current-system/sw/bin")
    first = _generation(store, 1, *RENDERER_KEYS)
    profile = _profile(first)
    config.save(config.Settings(roots=(library,), scan_workshop=False))
    target = paths.runtime_config_path()
    assert _write_config(capsys) == [f"wrote: {target}"]
    before = target.read_bytes()

    _switch(profile, _generation(store, 2, *RENDERER_KEYS))
    shutil.rmtree(first.parent / "001-mpvpaper")  # the old generation's renderer, collected
    assert _write_config(capsys) == [f"nothing changed: {target} is already current"]
    assert target.read_bytes() == before
    mpvpaper = runtime_config.find_program("mpvpaper")
    assert mpvpaper.found and mpvpaper.path == profile / "bin" / "mpvpaper"
    assert mpvpaper.path.resolve().is_relative_to(store / "002-mpvpaper")


def test_a_store_directory_on_path_never_shadows_an_installed_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `nix shell` in front of PATH is that terminal's, and nothing keeps it alive."""
    store = tmp_path / "store"
    monkeypatch.setattr(runtime_config, "NIX_STORE", store)
    # A name this machine has nowhere else, so each fallback below is this test's.
    name = "wio-renderer-in-a-nix-shell"
    shell = store / "abc-renderer" / "bin"
    _executable(shell, name)
    installed = _executable(Path.home() / ".nix-profile" / "bin", name)
    unusual = _executable(tmp_path / "opt" / "bin", name)
    monkeypatch.setenv("PATH", os.pathsep.join((str(shell), str(unusual.parent))))
    assert runtime_config.find_program(name) == runtime_config.Program(name, installed, found=True)
    installed.unlink()
    assert runtime_config.find_program(name).path == unusual, "then the rest of PATH"
    unusual.unlink()
    assert runtime_config.find_program(name) == runtime_config.Program(
        name, shell / name, found=True
    ), "and a store directory only as the last resort"


def test_the_search_order_is_profiles_system_path_then_store_directories(
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
    assert order[:2] == (
        home / ".nix-profile" / "bin",
        home / ".local" / "state" / "nix" / "profile" / "bin",
    )
    assert Path("/run/current-system/sw/bin") in order
    assert order[-8:] == (
        home / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        elsewhere,
        later,
        first,
        second,
    ), "then the rest of PATH, then its store directories, in PATH order, each place once"
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
    assert runtime_config._program(only_here.name) == only_here

    # And in a compiled runtime.toml, for each renderer this machine doesn't
    # already have in a standard place (in a build sandbox: all three).
    missing = {
        name: key
        for name, key in RENDERER_KEYS.items()
        if not any((directory / name).exists() for directory in standard)
    }
    installed = {name: _executable(custom, name) for name in missing}
    config.save(config.Settings(roots=(library,), scan_workshop=False))
    _write_config(capsys)
    renderer = paths.runtime_config_path().read_text(encoding="utf-8")
    for name, key in missing.items():
        assert f'{key} = "{installed[name]}"' in renderer


def test_find_program_says_whether_the_program_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The placeholder a missing renderer is written as is not how the app knows it is missing."""
    monkeypatch.setattr(runtime_config, "NIX_STORE", tmp_path / "store")
    monkeypatch.setenv("PATH", "/nonexistent/bin")
    name = "wio-renderer-nobody-installed"
    assert runtime_config.find_program(name) == runtime_config.Program(
        name, Path("/usr/bin") / name, found=False
    )
    assert runtime_config._program(name) == Path("/usr/bin") / name, "0.1.4's placeholder"

    # A link in ~/.local/bin is named as the link, so re-pointing it needs no recompile.
    target = _executable(tmp_path / "builds" / "one", name)
    link = Path.home() / ".local" / "bin" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    assert runtime_config.find_program(name) == runtime_config.Program(name, link, found=True)

    # A dangling link (its target collected) is not a program.
    target.unlink()
    assert not runtime_config.find_program(name).found
