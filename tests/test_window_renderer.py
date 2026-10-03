"""GPU acceleration: the renderer a GUI start leaves to GTK, decided before GTK loads.

No GTK here. `cli._choose_window_renderer` is checked against a plain mapping,
because the isolated wrapper and the gui-tests check pin ``GSK_RENDERER=cairo``
in the real environment; the GUI entry points are replaced by recording stubs,
as `test_ui_selection` does. The fresh-process half (GTK really starting under
each choice) is `test_window_renderer_process`.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from wall_in_one import cli, paths, ui_prefs
from wall_in_one.control import client


def _config_files() -> dict[str, bytes] | None:
    directory = paths.app_config_dir()
    if not directory.exists():
        return None
    return {entry.name: entry.read_bytes() for entry in directory.iterdir() if entry.is_file()}


@pytest.mark.parametrize(
    ("saved", "environ", "expected"),
    [
        (None, {}, {}),
        (True, {}, {}),
        (False, {}, {"GSK_RENDERER": "cairo"}),
        (False, {"GSK_RENDERER": "vulkan"}, {"GSK_RENDERER": "vulkan"}),
        (False, {"GSK_RENDERER": ""}, {"GSK_RENDERER": ""}),
        (True, {"GSK_RENDERER": "cairo"}, {"GSK_RENDERER": "cairo"}),
    ],
)
def test_the_renderer_is_gtks_own_choice_unless_gpu_acceleration_is_off(
    saved: bool | None, environ: dict[str, str], expected: dict[str, str]
) -> None:
    if saved is not None:
        ui_prefs.update({"gpu_acceleration": saved})
    before = _config_files()

    cli._choose_window_renderer(environ)

    assert environ == expected, "on (the default) sets nothing; an explicit value wins"
    assert _config_files() == before, "choosing writes nothing"


@pytest.mark.parametrize(
    "document",
    ["version = 9\ngpu_acceleration = false\n", "gpu_acceleration = false\n[[\n", "directory"],
)
def test_a_newer_or_unusable_ui_toml_leaves_the_renderer_to_gtk(document: str) -> None:
    target = paths.ui_prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if document == "directory":
        target.mkdir()
    else:
        target.write_text(document)
    before = _config_files()
    environ: dict[str, str] = {}

    cli._choose_window_renderer(environ)

    assert environ == {}
    assert _config_files() == before


def _install_gui(monkeypatch: pytest.MonkeyPatch, seen: list[tuple[str, str | None]]) -> None:
    """Record GSK_RENDERER where GTK could first load: the upgrade gate's recovery
    window and ui.app.run."""
    fake = types.ModuleType("wall_in_one.ui.app")

    def run(*_arguments: object, **_keywords: object) -> int:
        seen.append(("run", os.environ.get("GSK_RENDERER")))
        return 0

    fake.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wall_in_one.ui.app", fake)

    def gate(**_keywords: object) -> int | None:
        seen.append(("gate", os.environ.get("GSK_RENDERER")))
        return None

    monkeypatch.setattr(cli, "_run_graphical_startup_upgrade", gate)


@pytest.mark.parametrize("argv", [[], ["--service"], ["--open-page", "settings"], ["--ui=next"]])
@pytest.mark.parametrize(("gpu", "expected"), [(True, None), (False, "cairo")])
def test_every_gui_start_chooses_before_gtk_can_load(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], gpu: bool, expected: str | None
) -> None:
    monkeypatch.delenv("GSK_RENDERER", raising=False)  # restored after the test
    ui_prefs.update({"gpu_acceleration": gpu})
    seen: list[tuple[str, str | None]] = []
    _install_gui(monkeypatch, seen)

    assert cli.main(argv) == 0

    assert seen == [("gate", expected), ("run", expected)]


def _refuse(*_arguments: object, **_keywords: object) -> bool:
    raise AssertionError("a headless path read the GPU preference")


@pytest.mark.parametrize(
    "argv",
    [
        ["--write-config"],
        ["--service-startup-prepare"],
        ["--sync-runtime-health"],
        ["--sync-runtime-health-on-stop"],
        ["--update-status"],
        ["ctl", "status"],
        ["ctl", "open", "settings"],
    ],
)
def test_headless_paths_never_read_it(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(ui_prefs, "launch_gpu_acceleration", _refuse)
    stub: Callable[..., int] = lambda *_arguments, **_keywords: 0  # noqa: E731
    monkeypatch.setattr(cli, "_run_unattended_writer", stub)
    monkeypatch.setattr(cli, "_prepare_service_start", stub)
    monkeypatch.setattr(client, "dispatch", stub)
    monkeypatch.setitem(
        sys.modules,
        "wall_in_one.update_status",
        types.SimpleNamespace(report=lambda: {}),
    )

    assert cli.main(argv) == 0


def test_the_version_flag_never_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_prefs, "launch_gpu_acceleration", _refuse)
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0


def test_the_choice_module_is_imported_without_gtk() -> None:
    """The lookup must run before GTK loads, so its own imports must not load it."""
    assert "gi" not in vars(ui_prefs)
    assert Path(ui_prefs.__file__).name == "ui_prefs.py"
