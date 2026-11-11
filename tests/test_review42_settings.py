"""Codex review 0.4.2: Preview policy freshness (R42-1); no real profile or renderers."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.test_workshop_still_sources import _Tree
from wall_in_one import config, paths, runtime_config
from wall_in_one.library import workshop
from wall_in_one.session import Session


def _scene_ids(document: str) -> set[str]:
    raw = tomllib.loads(document)
    return {
        str(entry["scene_id"])
        for playlist in raw.get("playlists", [])
        for entry in playlist["entries"]
        if "scene_id" in entry
    }


def test_saved_view_rescans_when_saved_web_preview_policy_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = _Tree(tmp_path)
    monkeypatch.setattr(workshop, "steam_roots", lambda *_args, **_kwargs: (tree.steam,))
    old = config.Settings(
        roots=(tree.root,), allow_web_wallpapers=True, web_wallpaper_previews=True
    )
    config.save(old)
    accepted = Session(old, owns_playback=False)
    view: Session | None = None
    fresh: Session | None = None
    try:
        accepted.refresh(mutate_removals=False)
        assert accepted.library.previews
        assert {"300", "400"} <= _scene_ids(runtime_config.render(old, accepted))
        # Captured by GTK before a concurrent writer changes this source policy.
        read_now = accepted.saved_view()
        saved = config.update({"web_wallpaper_previews": False})
        assert saved.allow_web_wallpapers and not saved.preview_stills
        view = read_now()
        # Independent fresh-scan control, with the real scanner, not a closure
        # that accidentally keeps the old policy in an injected scanner.
        fresh = Session(saved, owns_playback=False)
        fresh.refresh(mutate_removals=False)
        expected = _scene_ids(runtime_config.render(saved, fresh))
        assert "200" in expected
        assert not {"300", "400"} & expected
        assert view.settings == saved
        actual = _scene_ids(runtime_config.render(view.settings, view))
        assert actual == expected, (
            "saved_view kept web preview stills from a Library scanned under "
            f"the old policy: actual={actual}, fresh={expected}, "
            f"library.previews={view.library.previews}"
        )
    finally:
        for session in (view, fresh, accepted):
            if session is not None:
                session.shutdown()


def test_turning_web_on_in_a_new_saved_profile_keeps_preview_opt_in_off() -> None:
    saved = config.update({"allow_web_wallpapers": True})
    assert saved.allow_web_wallpapers and not saved.preview_stills
    assert "web_wallpaper_previews = false" in paths.settings_path().read_text()
    assert config.load_strict() == saved


def test_reading_and_mutating_a_041_web_on_profile_keeps_preview_opt_in_on() -> None:
    target = paths.settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("roots = []\nallow_web_wallpapers = true\n", encoding="utf-8")
    assert config.load_strict().preview_stills
    saved = config.update({"shuffle": True})
    assert saved.preview_stills
    assert "web_wallpaper_previews" not in target.read_text()
