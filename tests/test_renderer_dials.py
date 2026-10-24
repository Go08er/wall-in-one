"""The renderer dials in runtime.toml: a typed allowlist, under schema 7, or left out.

docs/renderer-settings.md. Every dial is a typed, bounded value the app
formats itself, because mpvpaper hands ``-o`` to mpv as a config file with
every space a new line: nothing a person, a hand-edited file or a Workshop
item writes can become a second option. Untouched, nothing changes a byte;
an older service gets everything else.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.dials_fixtures import (
    EXCLUDED,
    INJECTIONS,
    bare_toml_values,
    entry,
    library,
    live_service,
    renderer_lines,
)
from wall_in_one import config, dials, paths, runtime_config
from wall_in_one.library import pairings


def test_no_option_a_dial_can_produce_is_outside_the_allowlist(tmp_path: Path) -> None:
    """Every value of every dial, global and own, compiled: only allowed keys and typed values."""
    settings, session, items = library(tmp_path)
    allowed = {dial.setting for dial in dials.GLOBAL} | {dial.key for dial in dials.DIALS}
    for dial in dials.DIALS:
        if dial.kind == "choice":
            values: list[dials.Value] = [value for value, _label in dial.choices]
        elif dial.kind == "bool":
            values = [True, False]
        else:
            values = [dial.minimum, dial.maximum, (dial.minimum + dial.maximum) / 2]
            if dial.kind == "int":
                values = [int(value) for value in values]
        for value in values:
            chosen = settings
            if dial.setting:
                changes: dict[str, Any] = {dial.setting: value}
                chosen = replace(settings, **changes)
            if dial.per_wallpaper:
                item = items["video"] if dial.renderer == "video" else items["scene"]
                session.pairings.set_dial(item, dial.renderer, dial.key, value)
            document = runtime_config.render(chosen, session)
            decoded = tomllib.loads(document)
            for key in decoded["renderer"]:
                assert key not in EXCLUDED
            for compiled in (
                found for playlist in decoded["playlists"] for found in playlist["entries"]
            ):
                for table in ("video", "scene"):
                    own = compiled.get(table, {})
                    assert isinstance(own, dict)
                    assert set(own) <= allowed, own
            for line in document.splitlines():
                if line.startswith(("video = ", "scene = ")):
                    for literal in bare_toml_values(line.split(" = ", 1)[1]):
                        assert re.fullmatch(r'-?[0-9.]+|true|false|"[a-z_0-9]*"', literal), line
            for word in EXCLUDED:
                assert not re.search(rf"(^|[ {{,]){re.escape(word)} =", document, re.M), word
        session.pairings.reset_renderer(items["video"])
        session.pairings.reset_renderer(items["scene"])


def test_an_excluded_option_cannot_get_in_through_any_input(tmp_path: Path) -> None:
    """settings.toml, a Settings built in code, pairings.json by hand, the Store, a property."""
    settings, session, items = library(tmp_path)
    target = tmp_path / "settings.toml"

    # settings.toml: refused when strict, the default when forgiving.
    for dial in config.DIAL_SETTINGS:
        for attempt in (
            *(json.dumps(text) for text in INJECTIONS if isinstance(text, str)),
            "nan",
            "inf",
            "[1]",
            '{ script = "/tmp/x.lua" }',
        ):
            target.write_text(f"{dial.setting} = {attempt}\n", encoding="utf-8")
            with pytest.raises(config.ConfigError, match=dial.setting):
                config.load_strict(target)
            assert getattr(config.load(target), dial.setting) == dial.default

    # A Settings built in code: repaired by `validated`, refused by the compiler if not.
    for dial in config.DIAL_SETTINGS:
        injected: dict[str, Any] = {dial.setting: "fill script=/tmp/x.lua"}
        raw = replace(settings, **injected)
        assert getattr(raw.validated(), dial.setting) == dial.default
        with pytest.raises(runtime_config.RuntimeConfigError, match=dial.setting):
            runtime_config.render(raw, session)

    # The Store: a dial a wallpaper has no business with, or a value it doesn't take.
    for renderer, key, value in (
        ("video", "script", "/tmp/x.lua"),
        ("video", "scale", "lanczos"),
        ("video", "fit", "fill include=/etc/passwd"),
        ("video", "speed", "1.5 script=/tmp/x.lua"),
        ("video", "speed", 99.0),
        ("scene", "fps", 0),
    ):
        item = items[renderer]
        with pytest.raises(ValueError):
            session.pairings.set_dial(item, renderer, key, value)
    with pytest.raises(ValueError):
        session.pairings.set_dial(items["video"], "scene", "fps", 60)
    for name, value in (("-x", "1"), ("motto", "a\n--screen-root x"), ("motto", "x" * 300)):
        with pytest.raises(ValueError):
            session.pairings.set_property(items["scene"], name, value)
    with pytest.raises(ValueError):
        session.pairings.set_property(items["web"], "motto", "hi")
    assert not paths.app_state_dir().joinpath(pairings.STATE_FILENAME).exists()

    # pairings.json by hand: nothing but a known, valid dial is ever compiled.
    state = tmp_path / "hand-edited" / pairings.STATE_FILENAME
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "pairings": [
                    {
                        "identity": pairings.Identity.of(items["video"]).key,
                        "renderer": {
                            "video": {
                                "fit": "fill include=/etc/passwd",
                                "speed": "1.5 script=/tmp/x.lua",
                                "script": "/tmp/x.lua",
                                "scale": "lanczos",
                                "rotate": 45,
                                "gamma": 12,
                            }
                        },
                    },
                    {
                        "identity": pairings.Identity.of(items["scene"]).key,
                        "renderer": {
                            "scene": {"fps": "60 --assets-dir=/tmp", "parallax": "no"},
                            "properties": {
                                "undeclared": "1",
                                "style": "c",
                                "speed": "99",
                                "tint": "255 0 0",
                                "motto": "hi\n--fork",
                            },
                        },
                    },
                    {
                        "identity": pairings.Identity.of(items["web"]).key,
                        "renderer": {"properties": {"motto": "hi"}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings, session, items = library(tmp_path, store=pairings.Store.open(state))
    assert session.pairings.fault is None
    document = runtime_config.render(settings, session)

    assert entry(document, "kind", "video")["video"] == {"gamma": 12}
    scene = entry(document, "scene_id", "1647046763")
    assert "scene" not in scene and "properties" not in scene
    assert "properties" not in entry(document, "scene_id", "2777242023")
    for word in EXCLUDED:
        assert not re.search(rf"(^|[ {{,]){re.escape(word)} =", document, re.M), word


def test_nothing_overridden_compiles_what_it_always_did(tmp_path: Path) -> None:
    settings, session, _items = library(tmp_path)

    document = runtime_config.render(settings, session)

    assert tomllib.loads(document)["schema_version"] == 6  # the preset's, as on 4c28b8b
    assert renderer_lines(document)[-6:] == [
        "scene_fps = 30",
        "scene_muted = true",
        "scene_volume = 0",
        "scene_pause_when_covered = true",
        'scene_scaling = ""',
        'scene_clamp = ""',
    ]
    assert not re.search(r"^(video|scene) = ", document, re.M)
    # And settings.toml keeps exactly its keys.
    assert not any(dial.setting in config.Settings().to_toml() for dial in config.DIAL_SETTINGS)
    # Explicitly the default is still the default: nothing written, nothing needs 7.
    default_speed = replace(settings, video_speed=1.0, scene_particles=True)
    assert runtime_config.render(default_speed, session) == document


def test_a_dial_left_out_is_never_the_document_a_dial_changed(tmp_path: Path) -> None:
    settings, session, _items = library(tmp_path)
    plain = runtime_config.render(settings, session)
    assert runtime_config.render(settings, session, renderer_dials=False) == plain


def test_globals_reach_renderer_and_an_override_wins_where_set(tmp_path: Path) -> None:
    settings, session, items = library(tmp_path)
    chosen = replace(settings, video_speed=2.0, video_deband=True, scene_particles=False)
    session.pairings.set_dial(items["video"], "video", "speed", 0.5)
    session.pairings.set_dial(items["scene"], "scene", "fps", 60)

    document = runtime_config.render(chosen, session)
    decoded = tomllib.loads(document)

    assert decoded["schema_version"] == runtime_config.DIALS_SCHEMA_VERSION
    renderer = decoded["renderer"]
    assert (renderer["video_speed"], renderer["video_deband"]) == (2.0, True)
    assert renderer["scene_particles"] is False
    assert entry(document, "kind", "video")["video"] == {"speed": 0.5}
    assert entry(document, "scene_id", "1647046763")["scene"] == {"fps": 60}
    assert "scene" not in entry(document, "scene_id", "2946165131"), "only its own"

    # Back to the default, one dial at a time, and all at once.
    session.pairings.set_dial(items["video"], "video", "speed", None)
    assert "video" not in entry(runtime_config.render(chosen, session), "kind", "video")
    session.pairings.set_dial(items["scene"], "scene", "mouse", False)
    assert session.pairings.reset_renderer(items["scene"])
    assert not session.pairings.reset_renderer(items["scene"])
    document = runtime_config.render(settings, session)
    assert tomllib.loads(document)["schema_version"] == 6
    assert (
        not paths.app_state_dir()
        .joinpath(pairings.STATE_FILENAME)
        .read_text(encoding="utf-8")
        .count("renderer")
    ), "a record holding nothing is not kept"


def test_a_scene_that_sounds_by_itself_gets_the_default_volume(tmp_path: Path) -> None:
    """While scenes are silent ``[renderer]`` says volume 0, as it always did."""
    settings, session, items = library(tmp_path)
    session.pairings.set_dial(items["scene"], "scene", "muted", False)

    document = runtime_config.render(replace(settings, scene_volume=35), session)

    assert entry(document, "scene_id", "1647046763")["scene"] == {"muted": False, "volume": 35}
    sounding = runtime_config.render(replace(settings, scene_muted=False, scene_volume=35), session)
    assert "scene_volume = 35" in sounding
    assert entry(sounding, "scene_id", "1647046763")["scene"] == {"muted": False}


def test_a_dial_that_would_do_nothing_is_not_written(tmp_path: Path) -> None:
    settings, session, _items = library(tmp_path)
    playing = replace(settings, video_when_hidden="play", video_hidden_trigger="max")
    never = replace(settings, scene_pause_when_covered=False, scene_pause_only_active=True)

    assert "video_hidden_trigger" not in runtime_config.render(playing, session)
    assert "scene_pause_only_active" not in runtime_config.render(never, session)


def test_a_scenes_own_values_go_over_a_presets_and_are_checked_against_it(tmp_path: Path) -> None:
    settings, session, items = library(tmp_path)
    store = session.pairings
    store.set_property(items["preset"], "bloom", "true")
    store.set_property(items["preset"], "speed", "2.5")
    store.set_property(items["scene"], "tint", "0 0.5 1")
    store.set_property(items["scene"], "style", "b")
    store.set_property(items["scene"], "motto", "good night")

    document = runtime_config.render(settings, session)

    assert entry(document, "scene_id", "2946165131")["properties"] == {
        "bloom": "true",
        "speed": "2.5",
    }
    assert entry(document, "scene_id", "1647046763")["properties"] == {
        "motto": "good night",
        "style": "b",
        "tint": "0.000000 0.500000 1.000000",
    }
    assert tomllib.loads(document)["schema_version"] == 6, "values are schema 6's"
    store.set_property(items["scene"], "style", None)
    assert "style" not in str(
        entry(runtime_config.render(settings, session), "scene_id", "1647046763")
    )


@pytest.mark.parametrize(
    ("schemas", "schema", "properties", "dials_applied"),
    (
        ([4, 5], 4, False, False),
        ([4, 5, 6], 6, True, False),
        ([4, 5, 6, 7], 7, True, True),
    ),
)
def test_an_older_service_gets_everything_but_what_it_cannot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schemas: list[int],
    schema: int,
    properties: bool,
    dials_applied: bool,
) -> None:
    settings, session, items = library(tmp_path)
    chosen = replace(settings, video_gamma=10)
    session.pairings.set_dial(items["video"], "video", "fit", "fit")
    session.pairings.set_property(items["scene"], "bloom", "false")
    status = live_service(monkeypatch, schemas)

    published = runtime_config.publish_changes(chosen, session)

    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert tomllib.loads(document)["schema_version"] == schema
    scene = entry(document, "scene_id", "1647046763")
    assert ("properties" in scene) is properties, "a plain scene plays either way"
    assert ("video" in entry(document, "kind", "video")) is dials_applied
    assert ("video_gamma = 10" in document) is dials_applied
    notes = {one.owner for one in published.left_out}
    assert (runtime_config.DIALS_OWNER in notes) is not dials_applied
    if not dials_applied:
        assert "Renderer settings" in runtime_config.left_out_notice(published.left_out)
        dial_note = next(o for o in published.left_out if o.owner == runtime_config.DIALS_OWNER)
        assert "units are current, to apply them" in dial_note.warning()
        assert "units are current" in runtime_config.left_out_notice(published.left_out)
    # The health sync compiles what was published for that service.
    rebuilt = runtime_config.render(
        chosen,
        session,
        presets=runtime_config.presets_supported(status),
        renderer_dials=runtime_config.dials_supported(status),
    )
    assert rebuilt == document


def test_one_wallpapers_dials_change_only_its_entries(tmp_path: Path) -> None:
    """So the service hands over only the renderer showing it (service/tests/service.rs)."""
    settings, session, items = library(tmp_path)
    session.pairings.set_dial(items["scene"], "scene", "fps", 50)
    before = runtime_config.render(settings, session).splitlines()
    session.pairings.set_dial(items["video"], "video", "speed", 1.25)
    after = runtime_config.render(settings, session).splitlines()

    changed = [line for line in after if line not in before]
    assert [line for line in changed if not line.startswith("config_generation")] == [
        "video = { speed = 1.25 }"
    ]


def test_the_health_sync_finds_a_document_published_without_the_dials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service listing 7 that restarted before the next publication: what it holds is still
    the document without the dials, and the health sync takes that one, not a mismatch."""
    settings, session, items = library(tmp_path)
    session.pairings.set_dial(items["video"], "video", "fit", "fit")
    live_service(monkeypatch, [4, 5, 6])
    runtime_config.publish_changes(settings, session)
    held = runtime_config.read_config_generation(paths.runtime_config_path())

    for schemas in ([4, 5, 6, 7], [4, 5, 6], [4, 5]):
        status = {"status_version": 2, "supported_config_schemas": schemas}
        found = runtime_config.compiled_as_published(settings, session, status, held)
        assert runtime_config.document_generation(found.document) == held, schemas
        assert "video = {" not in found.document
    # Nothing it could have published: the one the status calls for.
    status = {"status_version": 2, "supported_config_schemas": [4, 5, 6, 7]}
    other = runtime_config.compiled_as_published(settings, session, status, "0" * 64)
    assert 'video = { fit = "fit" }' in other.document


def test_a_dial_on_a_wallpaper_left_out_for_another_reason_is_not_said_to_need_a_service(
    tmp_path: Path,
) -> None:
    """The refusal that keeps it out comes first (web off, a program), as for presets."""
    from wall_in_one.library.model import Unplayable

    settings, session, items = library(tmp_path)
    program = replace(
        items["scene"],
        path=items["scene"].path.with_name("100"),
        scene="100",
        title="Trojan",
        workshop_type="application",
        runs_program=True,
        unplayable=Unplayable.RUNTIME,
        unplayable_reason="it is a program",
        scene_settings=(),
    )
    settings, session, items = library(tmp_path, program)
    session.pairings.set_dial(items["web"], "scene", "fps", 60)
    session.pairings.set_dial(program, "scene", "fps", 60)
    web_off = replace(settings, allow_web_wallpapers=False)

    left_out = runtime_config.compiled(web_off, session, renderer_dials=False).left_out

    assert runtime_config.DIALS_OWNER not in {one.owner for one in left_out}
    document = runtime_config.render(web_off, session)
    assert 'scene_id = "2777242023"' not in document and 'scene_id = "100"' not in document
    # With web on, the page plays, and its own frame rate waits for the service.
    left_out = runtime_config.compiled(settings, session, renderer_dials=False).left_out
    assert runtime_config.DIALS_OWNER in {one.owner for one in left_out}
