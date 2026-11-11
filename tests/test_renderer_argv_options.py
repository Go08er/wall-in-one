"""No argument the app gives mpvpaper or linux-wallpaperengine can be taken for an option.

review-f2 N6, N7, U5. mpvpaper reads its argv with getopt_long (it imports
it), which takes an operand that starts with '-' for an option wherever it
stands, so its operands follow `--`. linux-wallpaperengine has its own parser
and no `--` is relied on there: nothing it is given starts with '-'. The
layer is one value of a closed list, as in the service (`RENDERER_LAYERS`).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import paths, runtime_config
from wall_in_one.theme import noctalia
from wall_in_one.theme.noctalia import set_wallpaper as real_set_wallpaper
from wall_in_one.wallpaper import outputs, renderer, scenes


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="wio-argv-", dir="/tmp") as directory:
        yield Path(directory)


def _record_mpvpaper(
    monkeypatch: pytest.MonkeyPatch, runtime: Path, launched: list[list[str]]
) -> None:
    def popen(command: list[str], **_keywords: object) -> subprocess.Popen[bytes]:
        launched.append(command)
        raise OSError("not really starting mpvpaper")

    monkeypatch.setattr(paths, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(renderer, "is_available", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", popen)


def test_mpvpapers_operands_follow_the_end_of_its_options(
    tmp_path: Path, short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[list[str]] = []
    _record_mpvpaper(monkeypatch, short_runtime_dir, launched)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError):
        renderer.Renderer(output="DP-1").start(video)
    (command,) = launched
    assert command[-3:] == ["--", "DP-1", str(video)]
    assert command.count("--") == 1


@pytest.mark.parametrize("layer", ["--help", "-o", "bottom", "Background", "", "x"])
def test_a_layer_outside_the_closed_list_is_refused_by_both_renderers(
    tmp_path: Path, short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch, layer: str
) -> None:
    launched: list[list[str]] = []
    _record_mpvpaper(monkeypatch, short_runtime_dir, launched)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError, match="not a layer"):
        renderer.Renderer(layer=layer).start(video)
    with pytest.raises(scenes.SceneError, match="not a layer"):
        scenes.SceneRenderer(layer=layer).command("12345", available_outputs=("DP-1",))
    assert launched == []


def test_an_output_that_starts_with_a_dash_is_refused(
    tmp_path: Path, short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[list[str]] = []
    _record_mpvpaper(monkeypatch, short_runtime_dir, launched)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0")
    with pytest.raises(renderer.RendererError, match="starts with '-'"):
        renderer.Renderer(output="--help").start(video)
    with pytest.raises(scenes.SceneError, match="starts with '-'"):
        scenes.SceneRenderer(output="-o").command("12345")
    assert launched == []


def test_a_discovered_output_that_starts_with_a_dash_is_not_given_to_the_engine() -> None:
    arguments = scenes.SceneRenderer().command("12345", available_outputs=("-o", "DP-1"))
    assert arguments.count("--screen-root") == 1
    assert arguments[arguments.index("--screen-root") + 1] == "DP-1"
    with pytest.raises(scenes.SceneError, match="no display"):
        scenes.SceneRenderer().command("12345", available_outputs=("-o",))


@pytest.mark.parametrize("scene", ["-5", "--help", "12 3", "abc", "", "1/../2"])
def test_a_scene_is_given_to_the_engine_only_as_a_workshop_id(scene: str) -> None:
    with pytest.raises(scenes.SceneError, match="Workshop id"):
        scenes.SceneRenderer().command(scene, available_outputs=("DP-1",))


def test_the_apps_layers_are_the_services_and_the_only_one_it_writes() -> None:
    source = (Path(__file__).resolve().parents[1] / "service/src/config.rs").read_text()
    match = re.search(r"RENDERER_LAYERS: &\[&str\] = &\[([^\]]*)\]", source)
    assert match is not None
    assert re.findall(r'"([^"]*)"', match.group(1)) == list(renderer.LAYERS)
    assert renderer.LAYERS == scenes.LAYERS
    written = Path(runtime_config.__file__).read_text()
    assert re.findall(r"'layer = \"([^\"]*)\"'", written) == list(renderer.LAYERS)


def test_a_connector_that_starts_with_a_dash_is_data_to_the_compiler(tmp_path: Path) -> None:
    """review-f3 M1: refusing it made every compile fail once such a name was saved."""
    from dataclasses import replace

    from tests import test_runtime_config as compiled
    from wall_in_one import config
    from wall_in_one.ui.next import displays_settings

    cases: list[tuple[str, dict[str, str], dict[str, Any]]] = [
        ("assignment", {"DP-1": "evening", "-X": "evening"}, {"theme_source_connector": "DP-1"}),
        ("theme", {"DP-1": "evening"}, {"theme_source_connector": "-X"}),
        ("output", {"DP-1": "evening"}, {"theme_source_connector": "DP-1", "output": "-X"}),
    ]
    for label, assignments, changes in cases:
        (tmp_path / label).mkdir()
        settings, session = compiled._session(tmp_path / label, display_assignments=assignments)
        settings = replace(settings, display_mode=config.DISPLAY_MODE_INDEPENDENT, **changes)
        text = runtime_config.render(settings, session)
        assert "-X" in text or label == "output", label
    # Switching to independent displays proposes the first attached one, which
    # GTK may name '-X': the proposal must still compile.
    (tmp_path / "edit").mkdir()
    settings, session = compiled._session(tmp_path / "edit", display_assignments={})
    proposal = displays_settings.settings_edit(
        settings, {"display_mode": config.DISPLAY_MODE_INDEPENDENT}, ["-X", "DP-1"]
    )
    assert isinstance(proposal, dict) and proposal["theme_source_connector"] == "-X"
    runtime_config.render(replace(settings, **proposal), session)


def test_a_display_whose_name_starts_with_a_dash_is_not_discovered() -> None:
    """review-f4 F4-1: such a head is not present, from the service's list or GTK's."""
    status = {"outputs": [{"name": "-X", "enabled": True}, {"name": "DP-1", "enabled": True}]}
    assert outputs.names(outputs.from_status(status) or ()) == ("DP-1",)

    class Monitor:
        def __init__(self, connector: str) -> None:
            self.connector = connector

        def get_connector(self) -> str:
            return self.connector

    found = outputs.from_monitors([Monitor("--help"), Monitor("eDP-1")])
    assert outputs.names(found) == ("eDP-1",)


def test_noctalia_is_never_given_a_display_name_that_starts_with_a_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review-f4 F4-12: `wallpaper-set <connector> <path>` takes the name as an operand."""
    sent: list[tuple[str, ...]] = []

    def message(command: str, *arguments: str, **_keywords: object) -> str:
        sent.append((command, *arguments))
        return ""

    monkeypatch.setattr(noctalia, "message", message)
    still = tmp_path / "still.png"
    for name in ("-X", "--help"):
        with pytest.raises(noctalia.NoctaliaError, match="starts with '-'"):
            real_set_wallpaper(still, name)
        with pytest.raises(noctalia.NoctaliaError, match="starts with '-'"):
            noctalia.current_wallpaper(name)
    assert sent == []
    real_set_wallpaper(still, "DP-1")
    real_set_wallpaper(still)
    assert sent == [("wallpaper-set", "DP-1", str(still)), ("wallpaper-set", str(still))]
