"""A web wallpaper never plays after "Allow web wallpapers" is turned off, via a kept runtime.

A ``runtime.toml`` compiled while the setting was on still names the web page,
and the service can't tell one from a scene. When the compile that would
replace it fails (a library folder that can't be read), service start keeps
the last-known-good document, so it must not keep one that plays what the
person turned off. Steam's folder is a fixture under the test's own home.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.session import Session

WEB = "2777242023"


def _web_profile(home: Path) -> Path:
    """A web wallpaper (with its still) under the home's Steam, and a library folder."""
    item = home / ".local/share/Steam/steamapps/workshop/content/431960" / WEB
    item.mkdir(parents=True)
    (item / "project.json").write_text(
        json.dumps(
            {"type": "web", "title": "Electric Eye", "file": "index.html", "preview": "p.jpg"}
        )
    )
    (item / "index.html").write_text("<p>x</p>")
    (item / "p.jpg").write_bytes(b"x")
    root = home / "Pictures/wallpapers"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    (automatic / f"{WEB}.png").write_bytes(b"\x89PNG")
    return root


def _compiled_with_web(root: Path) -> None:
    settings = config.Settings(roots=(root,), allow_web_wallpapers=True)
    config.save(settings)
    session = Session(settings)
    session.refresh()
    runtime_config.publish_changes(settings, session)
    session.shutdown()
    assert WEB in paths.runtime_config_path().read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
def test_start_does_not_keep_a_runtime_that_plays_web_while_they_are_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before: exit 0, "validating the last-known-good runtime", and the web page still in it."""
    root = _web_profile(Path(os.environ["HOME"]))
    _compiled_with_web(root)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    root.chmod(0)
    try:
        code = cli._prepare_service_start()
    finally:
        root.chmod(0o755)
    said = capsys.readouterr().err

    assert code == cli.EXIT_CONFIG, said  # not 1: the unit would restart the service on it
    assert "Electric Eye" in said and "Allow web wallpapers" in said
    assert "validating the last-known-good runtime" not in said


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
def test_start_keeps_a_runtime_that_plays_no_web_page(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Control: the setting is on (or the document has no web page), and the old rule holds."""
    root = _web_profile(Path(os.environ["HOME"]))
    _compiled_with_web(root)
    root.chmod(0)
    try:
        code = cli._prepare_service_start()
    finally:
        root.chmod(0o755)
    assert code == 0
    assert "validating the last-known-good runtime" in capsys.readouterr().err
