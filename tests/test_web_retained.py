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

from tests import workshop_picks
from tests.test_web_off_nothing_to_play import _service
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
    # A web page has no automatic still: its one is a still the person picked.
    workshop_picks.save(workshop_picks.pick(WEB, root / "Wall-in-One" / "picked-electric-eye.png"))
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

    assert code == cli.EXIT_CONFIG, said  # 78, not 1: the unit does not restart it on 78
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


# -- the guard fails closed where the scan can't classify (review of r11 fix 2, L1) -------


def _kept_web_with_the_library_unreadable(damage: str) -> tuple[int, str]:
    """Compile with web on, turn it off, damage Steam's side as `damage` says, run the step."""
    home = Path(os.environ["HOME"])
    root = _web_profile(home)
    steam = home / ".local/share/Steam"
    item = steam / "steamapps/workshop/content/431960" / WEB
    _compiled_with_web(root)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    locked = [root]
    if damage == "steam_unreadable":
        locked.append(steam)
    elif damage == "project_large":
        (item / "project.json").write_text(
            json.dumps(
                {"type": "web", "title": "Electric Eye", "file": "index.html", "preview": "p.jpg"}
                | {"description": "x" * (300 * 1024)}
            )
        )
    elif damage == "item_gone":
        for leftover in item.iterdir():
            leftover.unlink()
        item.rmdir()
    for path in locked:
        path.chmod(0)
    try:
        code = cli._prepare_service_start()
    finally:
        for path in reversed(locked):
            path.chmod(0o755)
    return code, WEB


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
@pytest.mark.parametrize("damage", ("steam_unreadable", "project_large", "item_gone"))
def test_start_refuses_a_kept_runtime_whose_scene_the_scan_cannot_classify(
    damage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before: exit 0, the kept document (with the web page) validated and started."""
    code, web = _kept_web_with_the_library_unreadable(damage)
    said = capsys.readouterr().err

    assert code == cli.EXIT_CONFIG, said
    assert web in paths.runtime_config_path().read_text()
    assert "could not be checked" in said and web in said
    assert "validating the last-known-good runtime" not in said


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
def test_unchecked_scene_does_not_stop_a_start_with_web_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Web on: nothing changes, even where the scan is blind."""
    home = Path(os.environ["HOME"])
    root = _web_profile(home)
    steam = home / ".local/share/Steam"
    _compiled_with_web(root)
    root.chmod(0)
    steam.chmod(0)
    try:
        code = cli._prepare_service_start()
    finally:
        steam.chmod(0o755)
        root.chmod(0o755)
    assert code == 0
    assert "validating the last-known-good runtime" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
def test_refusal_says_to_start_the_service_again(capsys: pytest.CaptureFixture[str]) -> None:
    root = _web_profile(Path(os.environ["HOME"]))
    _compiled_with_web(root)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    root.chmod(0)
    try:
        cli._prepare_service_start()
    finally:
        root.chmod(0o755)
    said = capsys.readouterr().err
    assert "remove the web wallpaper" in said and "start the service again" in said


# -- a kept document that allows web wallpapers (review r13, L4) ------------------------------

SCENE = "1111111111"


def _scene_profile(home: Path) -> Path:
    """A Workshop scene (with its still) under the home's Steam, and a library folder."""
    item = home / ".local/share/Steam/steamapps/workshop/content/431960" / SCENE
    item.mkdir(parents=True)
    (item / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Toothless", "file": "scene.json"})
    )
    (item / "scene.json").write_text("{}")
    root = home / "Pictures/wallpapers"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    (automatic / f"{SCENE}.png").write_bytes(b"\x89PNG")
    return root


def _kept_scenes(monkeypatch: pytest.MonkeyPatch, *, allow_web: bool) -> Path:
    """Scenes compiled for a service that reads 8, with web on or off; then web turned off."""
    root = _scene_profile(Path(os.environ["HOME"]))
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8], units="current")
    settings = config.Settings(roots=(root,), allow_web_wallpapers=allow_web)
    config.save(settings)
    session = Session(settings)
    try:
        session.refresh()
        runtime_config.publish_changes(settings, session)
    finally:
        session.shutdown()
    text = paths.runtime_config_path().read_text()
    assert SCENE in text and ("allow_web_wallpapers = true" in text) is allow_web
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    return root


def test_a_kept_document_that_allows_web_wallpapers_is_refused_while_they_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's probe. Before: "" for a document that allows web wallpapers."""
    _kept_scenes(monkeypatch, allow_web=True)
    refusal = cli._retained_web_refusal()
    assert "allows web wallpapers" in refusal and "Allow web wallpapers is off" in refusal


def test_control_a_kept_document_without_the_key_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _kept_scenes(monkeypatch, allow_web=False)
    assert cli._retained_web_refusal() == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable folder")
@pytest.mark.parametrize("allow_web", [True, False], ids=("allows web", "control"))
def test_start_does_not_keep_a_runtime_that_allows_web_while_they_are_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], allow_web: bool
) -> None:
    """Before: exit 0, the service started on a document that names only scenes but allows web
    wallpapers, so a Workshop update that made one of them a web page would play it."""
    root = _kept_scenes(monkeypatch, allow_web=allow_web)
    _service(monkeypatch, schemas=None)  # not running yet: this is its ExecStartPre
    root.chmod(0)
    try:
        code = cli._prepare_service_start()
    finally:
        root.chmod(0o755)
    said = capsys.readouterr().err
    if not allow_web:
        assert code == 0, said
        assert "validating the last-known-good runtime" in said
        return
    assert code == cli.EXIT_CONFIG, said
    assert "allows web wallpapers" in said and "start the service again" in said
    assert "validating the last-known-good runtime" not in said
    # Kept as it is: nothing was started on it, and nothing else was written.
    assert "allow_web_wallpapers = true" in paths.runtime_config_path().read_text()
