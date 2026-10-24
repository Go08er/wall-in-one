"""The dials left out because the loaded units can't be verified: the health sync still agrees.

Publishing leaves the dials out unless the service lists schema 7 *and* its
loaded units are verified (`runtime_compatibility.require_schema`), and then
presets too unless 6 is confirmed. r11's health sync looked for a document
with or without presets only, so with a dial set it rebuilt one with the
dials, never matched what was published, and wrote no quarantine marker.
Steam is a fixture under the test's home and the service is faked.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_presets_unverified_units import _health, _profile
from wall_in_one import cli, config, paths, runtime_config, update_status
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import pairings
from wall_in_one.session import Session


def _service(monkeypatch: pytest.MonkeyPatch, *, units: str) -> dict[str, str]:
    """A service that lists schema 7, with its loaded units ``units``."""
    answers: dict[str, str] = {}

    def send(verb: str, *_a: object, **_k: object) -> Response:
        if verb == "status":
            return Response.success(
                json.dumps(
                    {
                        "status_version": 2,
                        "supported_config_schemas": [4, 5, 6, 7],
                        **_health(answers),
                    }
                )
            )
        return Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(
        update_status, "inspect_loaded_units", lambda **_k: update_status.LoadedUnits(units, "t")
    )
    return answers


def test_the_health_sync_marks_a_wallpaper_when_the_dials_were_left_out_for_unverified_units(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without: "runtime status is from configuration generation ...", and no marker."""
    settings, item = _profile(Path(os.environ["HOME"]))
    settings = replace(settings, video_gamma=10, scene_particles=False)
    config.save(settings)
    answers = _service(monkeypatch, units="unverified")
    session = Session(settings)
    session.refresh()
    published = runtime_config.publish_changes(settings, session)
    session.shutdown()
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    assert document["schema_version"] == 4, "published without the dials and the presets"
    assert "video_gamma" not in document["renderer"]
    owners = {one.owner for one in published.left_out}
    assert owners == {runtime_config.DIALS_OWNER, runtime_config.PRESET_OWNER}, "and said so"

    answers["item"] = str(item.path)
    capsys.readouterr()
    code = cli.main(["--sync-runtime-health"])
    said = capsys.readouterr().err

    assert code == 0, said
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
