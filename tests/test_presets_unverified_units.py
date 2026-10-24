"""Presets left out because the loaded units can't be verified: the health sync still agrees.

Publishing leaves presets out unless the service lists schema 6 *and* its
loaded units are verified (`runtime_compatibility.require_schema`). The health
sync used to look only at the schemas, so for a new service with unverified
units it rebuilt a document with presets, never matched what was published,
and wrote no quarantine marker. The fixture is Steam under the test's home and
a faked service; no socket is opened.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from wall_in_one import cli, config, paths, runtime_config, update_status
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session

SCENE, PRESET = "1647046763", "3000000001"


def _profile(home: Path) -> tuple[config.Settings, MediaItem]:
    content = home / ".local/share/Steam/steamapps/workshop/content/431960"
    for workshop_id, project in (
        (
            SCENE,
            {
                "type": "scene",
                "title": "Toothless",
                "file": "scene.json",
                "preview": "p.jpg",
                "general": {"properties": {"bloom": {"type": "bool", "value": False}}},
            },
        ),
        (
            PRESET,
            {"title": "Bloom", "dependency": SCENE, "preview": "p.gif", "preset": {"bloom": True}},
        ),
    ):
        (content / workshop_id).mkdir(parents=True)
        (content / workshop_id / "project.json").write_text(json.dumps(project))
    root = home / "Pictures/wallpapers"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    for workshop_id in (SCENE, PRESET):
        (automatic / f"{workshop_id}.png").write_bytes(b"\x89PNG")
    picture = root / "still.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    settings = config.Settings(roots=(root,))
    config.save(settings)
    info = picture.stat()
    return settings, MediaItem(picture, Kind.STILL, info.st_size, int(info.st_mtime))


def _service(monkeypatch: pytest.MonkeyPatch, *, units: str) -> dict[str, str]:
    """A service that lists schema 6, with its loaded units ``units``; ``answers`` is its status."""
    answers: dict[str, str] = {}

    def send(verb: str, *_a: object, **_k: object) -> Response:
        if verb == "status":
            return Response.success(
                json.dumps(
                    {"status_version": 2, "supported_config_schemas": [4, 5, 6], **_health(answers)}
                )
            )
        return Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(
        update_status, "inspect_loaded_units", lambda **_k: update_status.LoadedUnits(units, "t")
    )
    return answers


def _health(answers: dict[str, str]) -> dict[str, object]:
    if "item" not in answers:
        return {}
    path = answers["item"]
    return {
        "config_generation": runtime_config.read_config_generation(),
        "config_path": str(paths.runtime_config_path().absolute()),
        "runtime_instance": "a" * 32,
        "config_epoch": 1,
        "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
        "playlist": runtime_config.FALLBACK_PLAYLIST_NAME,
        "source": "schedule",
        "taboo_entries": [
            {
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "entry_id": runtime_config.entry_id_for_source(Path(path)),
                "reason": "renderer rejected this wallpaper",
                "source": "automatic-apply",
                "durable": False,
                "observed_config_epoch": 1,
            }
        ],
        "taboo_entries_omitted": 0,
    }


def test_the_health_sync_marks_a_wallpaper_when_presets_were_left_out_for_unverified_units(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before: "runtime status is from configuration generation ...", and no marker."""
    settings, item = _profile(Path(os.environ["HOME"]))
    answers = _service(monkeypatch, units="unverified")
    session = Session(settings)
    session.refresh()
    published = runtime_config.publish_changes(settings, session)
    session.shutdown()
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    assert document["schema_version"] == 4, "published without presets"
    assert published.left_out, "and said so"

    answers["item"] = str(item.path)
    capsys.readouterr()
    code = cli.main(["--sync-runtime-health"])
    said = capsys.readouterr().err

    assert code == 0, said
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked


def test_the_left_out_notice_does_not_blame_a_missing_restart() -> None:
    notice = runtime_config.left_out_notice(
        [
            runtime_config.LeftOut(
                runtime_config.PRESET_OWNER,
                PRESET,
                "Bloom",
                "",
                runtime_config.NEEDS_NEWER_SERVICE,
            )
        ]
    )
    assert "units are current" in notice and "restart it after an update" in notice
    assert "the one running doesn" not in notice
