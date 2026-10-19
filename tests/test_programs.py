"""The window and the compiler agree about where Noctalia and the renderers are.

From 0.4.0 the renderers are not bundled, so no wrapper puts them on the
window's ``PATH``. A window started from a launcher, or by a user unit whose
``PATH`` lacks the profile (standalone home-manager on another distribution,
a ``nix profile`` install without the module), must still find the renderer
that ``runtime.toml`` names: otherwise Settings says the scene engine is not
installed, no scene still is captured, and those entries are left out of the
document the service would have played them from. Every caller asks
`programs.find_program`, and starts the program by the path it answers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wall_in_one import programs, runtime_config
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer, scenes
from wall_in_one.wallpaper.scenes import SceneRenderer

ROOT = Path(__file__).resolve().parents[1]


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return program


@pytest.fixture
def profile_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Noctalia and both renderers in ``~/.nix-profile``; the process PATH has none of them."""
    monkeypatch.setattr(programs, "NIX_STORE", tmp_path / "store")
    profile = Path.home() / ".nix-profile" / "bin"
    for name in ("noctalia", "mpvpaper", "linux-wallpaperengine"):
        _executable(profile, name)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return profile


def test_the_window_says_installed_about_what_runtime_toml_names(profile_only: Path) -> None:
    for name in ("noctalia", "mpvpaper", "linux-wallpaperengine"):
        found = programs.find_program(name)
        assert found == programs.Program(name, profile_only / name, found=True)
        assert runtime_config._program(name) == found.path
    assert scenes.is_available(), "Settings -> Scenes and scene stills"
    assert renderer.is_available(), "the legacy applier would refuse a video the service plays"
    assert noctalia.is_available()
    assert noctalia._executable() == str(profile_only / "noctalia")


def test_and_says_not_installed_when_it_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where a test can say it: no renderer in a profile or a system place of this machine."""
    monkeypatch.setattr(programs, "NIX_STORE", tmp_path / "store")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    looked = programs.program_directories()
    if any((d / name).exists() for d in looked for name in ("mpvpaper", "linux-wallpaperengine")):
        pytest.skip("this machine has a renderer installed in a standard place")
    assert not scenes.is_available()
    assert not renderer.is_available()


def test_a_scene_is_started_by_the_path_runtime_toml_names(profile_only: Path) -> None:
    for arguments in (
        SceneRenderer().command("12345", available_outputs=("eDP-1",)),
        SceneRenderer().command("12345", screenshot=Path("/tmp/x.png"), window=(64, 40)),
    ):
        assert arguments[0] == str(profile_only / "linux-wallpaperengine")


def test_a_legacy_video_is_started_by_the_path_runtime_toml_names(
    profile_only: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def capture(command: list[str], **_kwargs: object) -> object:
        seen.append(command)
        raise OSError("not really starting mpvpaper")

    monkeypatch.setattr(subprocess, "Popen", capture)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError):
        renderer.Renderer().start(video)
    assert seen[0][0] == str(profile_only / "mpvpaper")


# -- what Settings says when the scene engine is missing --------------------------------


def test_the_missing_engine_advice_names_where_to_get_it() -> None:
    advice = programs.SCENE_ENGINE_MISSING
    for named in (
        "programs.wall-in-one",
        "manualWallpaperInstalls",
        "nix profile install nixpkgs#linux-wallpaperengine",
        "linux-wallpaperengine-git",
        "docs/installing.md",
    ):
        assert named in advice, named
    assert "wrapper" not in advice and "Nix package" not in advice
    installing = (ROOT / "docs" / "installing.md").read_text(encoding="utf-8")
    assert "nixpkgs#linux-wallpaperengine" in installing
    assert "linux-wallpaperengine-git" in installing


def test_neither_interface_keeps_the_old_advice() -> None:
    for relative in ("src/wall_in_one/ui/preferences.py", "src/wall_in_one/ui/next/settings.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "SCENE_ENGINE_MISSING" in source, relative
        assert "packaged wrapper" not in source, relative
