"""A document stepped down for an older service must not give sound to a scene
the person silenced for itself, nor re-enable the mouse on a web page they turned it off for.

In every document publishing makes (all, without dials, without dials+presets),
the effective `muted` of a scene whose own `muted = true` is true.
The [4, 5, 6, 7] service is the control.
"""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from tests.dials_fixtures import entry, library, live_service
from wall_in_one import paths, runtime_config


def _effective(document: str, scene_id: str, key: str, renderer_key: str) -> object:
    decoded = tomllib.loads(document)
    own = entry(document, "scene_id", scene_id).get("scene", {})
    assert isinstance(own, dict)
    return own.get(key, decoded["renderer"].get(renderer_key, True))


@pytest.mark.parametrize("schemas", [[4, 5], [4, 5, 6], [4, 5, 6, 7]])
def test_a_scene_silenced_for_itself_stays_silent_for_every_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schemas: list[int]
) -> None:
    settings, session, items = library(tmp_path)
    # Scenes have sound by default now; this one the person silenced for itself.
    chosen = replace(settings, scene_muted=False, scene_volume=80)
    session.pairings.set_dial(items["scene"], "scene", "muted", True)
    live_service(monkeypatch, schemas)

    runtime_config.publish_changes(chosen, session)

    document = paths.runtime_config_path().read_text(encoding="utf-8")
    muted = _effective(document, "1647046763", "muted", "scene_muted")
    assert muted is True, (
        f"service {schemas}: the scene the person silenced plays with sound "
        f"(renderer: scene_muted={tomllib.loads(document)['renderer']['scene_muted']}, "
        f"scene_volume={tomllib.loads(document)['renderer']['scene_volume']}; "
        f"entry scene table: {entry(document, 'scene_id', '1647046763').get('scene')})"
    )


@pytest.mark.parametrize("schemas", [[4, 5], [4, 5, 6], [4, 5, 6, 7]])
def test_a_quieter_scene_is_not_played_louder_for_any_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schemas: list[int]
) -> None:
    settings, session, items = library(tmp_path)
    chosen = replace(settings, scene_muted=False, scene_volume=100)
    session.pairings.set_dial(items["scene"], "scene", "volume", 5)
    live_service(monkeypatch, schemas)

    runtime_config.publish_changes(chosen, session)

    document = paths.runtime_config_path().read_text(encoding="utf-8")
    decoded = tomllib.loads(document)
    own = entry(document, "scene_id", "1647046763").get("scene", {})
    assert isinstance(own, dict)
    muted = own.get("muted", decoded["renderer"]["scene_muted"])
    volume = own.get("volume", decoded["renderer"]["scene_volume"])
    assert muted is True or volume <= 5, (
        f"service {schemas}: plays at volume {volume} a scene set to 5 for itself"
    )


@pytest.mark.parametrize("schemas", [[4, 5], [4, 5, 6]])
def test_global_scene_sound_waits_with_the_dials_and_the_notice_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schemas: list[int]
) -> None:
    """Silent off alone, with no dial changed, holds sound back and says so."""
    settings, session, _items = library(tmp_path)
    live_service(monkeypatch, schemas)

    compiled = runtime_config._compile(
        replace(settings, scene_muted=False, scene_volume=80), session, renderer_dials=False
    )
    renderer = tomllib.loads(compiled.document)["renderer"]
    assert (renderer["scene_muted"], renderer["scene_volume"]) == (True, 0)
    assert any(one.owner == runtime_config.DIALS_OWNER for one in compiled.left_out)

    silent = runtime_config._compile(settings, session, renderer_dials=False)
    assert not any(one.owner == runtime_config.DIALS_OWNER for one in silent.left_out)
    assert silent.document == runtime_config._compile(settings, session).document


def test_the_health_sync_still_finds_the_document_without_the_dials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, session, _items = library(tmp_path)
    chosen = replace(settings, scene_muted=False, scene_volume=80)
    live_service(monkeypatch, [4, 5])
    runtime_config.publish_changes(chosen, session)
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    status = {"status_version": 2, "supported_config_schemas": [4, 5]}

    found = runtime_config.compiled_as_published(
        chosen, session, status, runtime_config.document_generation(document)
    )
    assert found.document == document
