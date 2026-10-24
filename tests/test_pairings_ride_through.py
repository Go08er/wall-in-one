"""A dial this build doesn't know rides through (docs/renderer-settings.md, Storage).

A record that holds nothing this build models apart from its own dials is popped when
those go back to the default (per-dial reset, Use defaults) or when its health marker is
cleared, and the unknown dial of a newer build goes with it. Desired: it survives.
Control: the same edit on a record that also has a palette keeps it (passes on da139b5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.dials_fixtures import library
from wall_in_one import paths
from wall_in_one.library import pairings
from wall_in_one.library.model import MediaItem


def _seed(
    tmp_path: Path, record: dict[str, object]
) -> tuple[Path, pairings.Store, dict[str, MediaItem]]:
    _settings, _session, items = library(tmp_path)
    state = paths.app_state_dir() / pairings.STATE_FILENAME
    state.parent.mkdir(parents=True, exist_ok=True)
    key = pairings.Identity.of(items["video"]).key
    state.write_text(
        json.dumps({"version": 2, "pairings": [{"identity": key, **record}]}), encoding="utf-8"
    )
    return state, pairings.Store.open(state), items


def _renderer(state: Path) -> object:
    records = json.loads(state.read_text(encoding="utf-8"))["pairings"]
    return records[0].get("renderer") if records else None


@pytest.mark.parametrize("edit", ["per-dial reset", "use defaults", "retry"])
def test_a_newer_builds_dial_survives_when_this_builds_dials_go(tmp_path: Path, edit: str) -> None:
    state, store, items = _seed(tmp_path, {"renderer": {"video": {"speed": 1.5, "zoom": 2}}})
    video = items["video"]
    if edit == "per-dial reset":
        store.set_dial(video, "video", "speed", None)
    elif edit == "use defaults":
        store.reset_renderer(video)
    else:
        store.set_dial(video, "video", "speed", None)
        assert _renderer(state) == {"video": {"zoom": 2}}
        store.mark_borked(video, "it crashed", "mpvpaper")
        store.clear_borked(video)
    assert _renderer(state) == {"video": {"zoom": 2}}, (
        f"{edit}: the newer build's dial is gone: {state.read_text(encoding='utf-8')}"
    )


def test_control_a_record_with_a_palette_keeps_the_newer_dial(tmp_path: Path) -> None:
    state, store, items = _seed(
        tmp_path, {"palette": "adaptive", "renderer": {"video": {"speed": 1.5, "zoom": 2}}}
    )
    store.set_dial(items["video"], "video", "speed", None)
    assert _renderer(state) == {"video": {"zoom": 2}}


def test_retry_on_a_record_holding_only_a_newer_dial(tmp_path: Path) -> None:
    state, store, items = _seed(tmp_path, {"renderer": {"video": {"zoom": 2}}})
    store.mark_borked(items["video"], "it crashed", "mpvpaper")
    assert _renderer(state) == {"video": {"zoom": 2}}  # kept while borked
    store.clear_borked(items["video"])
    assert _renderer(state) == {"video": {"zoom": 2}}, state.read_text(encoding="utf-8")


def test_another_field_a_newer_build_wrote_keeps_the_record_too(tmp_path: Path) -> None:
    state, store, items = _seed(tmp_path, {"renderer": {"video": {"speed": 1.5}}, "note": "x"})
    store.reset_renderer(items["video"])
    records = json.loads(state.read_text(encoding="utf-8"))["pairings"]
    assert [record.get("note") for record in records] == ["x"]


def test_reset_colours_keeps_a_record_holding_only_a_newer_field(tmp_path: Path) -> None:
    state, store, items = _seed(tmp_path, {"palette": "adaptive", "note": "x"})
    store.reset(items["video"])
    records = json.loads(state.read_text(encoding="utf-8"))["pairings"]
    assert [record.get("note") for record in records] == ["x"]


def test_nothing_unknown_still_removes_the_record(tmp_path: Path) -> None:
    state, store, items = _seed(tmp_path, {"renderer": {"video": {"speed": 1.5}}})
    store.reset_renderer(items["video"])
    assert json.loads(state.read_text(encoding="utf-8"))["pairings"] == []


def test_the_judgement_is_on_the_file_read_under_the_lock(tmp_path: Path) -> None:
    state, store, items = _seed(tmp_path, {"renderer": {"video": {"speed": 1.5}}})
    # Another program adds a dial after this store read the file.
    key = pairings.Identity.of(items["video"]).key
    state.write_text(
        json.dumps(
            {
                "version": 2,
                "pairings": [{"identity": key, "renderer": {"video": {"speed": 1.5, "zoom": 2}}}],
            }
        ),
        encoding="utf-8",
    )
    store.reset_renderer(items["video"])
    assert _renderer(state) == {"video": {"zoom": 2}}
