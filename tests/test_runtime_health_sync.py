"""The daemon-only health bridge stays headless, atomic and app-owned."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem


def _media(tmp_path: Path) -> MediaItem:
    root = tmp_path / "library"
    root.mkdir()
    picture = root / "paper.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    config.save(config.Settings(roots=(root,), scan_workshop=False))
    metadata = picture.stat()
    return MediaItem(
        path=picture,
        kind=Kind.STILL,
        size=metadata.st_size,
        mtime=int(metadata.st_mtime),
    )


def _snapshot(item: MediaItem, *, reports: bool = True, omitted: int = 0) -> Response:
    taboo = (
        [
            {
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "entry_id": runtime_config.entry_id_for_source(item.path),
                "reason": "renderer rejected this wallpaper",
                "source": "automatic-apply",
            }
        ]
        if reports
        else []
    )
    return Response.success(
        json.dumps(
            {
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "playlist": runtime_config.FALLBACK_PLAYLIST_NAME,
                "source": "schedule",
                "taboo_entries": taboo,
                "taboo_entries_omitted": omitted,
            }
        )
    )


def test_health_sync_returns_not_running_before_reading_authoring_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "wall_in_one.ui.app", raising=False)
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: (_ for _ in ()).throw(client.NotRunningError("runtime absent")),
    )

    assert cli.main(["--sync-runtime-health"]) == client.EXIT_NOT_RUNNING
    assert not pairings.state_path().exists()
    assert not paths.runtime_config_path().exists()
    assert "wall_in_one.ui.app" not in sys.modules


def test_health_sync_persists_compiles_and_reloads_one_new_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    calls: list[str] = []

    def send(verb: str) -> Response:
        calls.append(verb)
        return _snapshot(item) if verb == "status" else Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.delitem(sys.modules, "wall_in_one.ui.app", raising=False)

    assert cli.main(["--sync-runtime-health"]) == 0
    assert calls == ["status", "reload"]
    health = pairings.Store.open().health(pairings.Identity.of(item))
    assert health.is_borked
    assert health.reason == "renderer rejected this wallpaper"
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    entry = document["playlists"][0]["entries"][0]
    assert entry["taboo"] == {
        "reason": "renderer rejected this wallpaper",
        "source": "automatic-apply",
    }
    assert "wall_in_one.ui.app" not in sys.modules


def test_health_sync_does_not_compile_or_clear_when_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    pairings.Store.open().mark_borked(
        item,
        "renderer rejected this wallpaper",
        "automatic-apply",
    )
    monkeypatch.setattr(client, "send_runtime", lambda _verb: _snapshot(item))
    monkeypatch.setattr(
        runtime_config,
        "update",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unchanged health compiled")),
    )

    assert cli.main(["--sync-runtime-health"]) == 0
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked

    # A bounded snapshot with only omitted identities is not proof of recovery.
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: _snapshot(item, reports=False, omitted=3),
    )
    assert cli.main(["--sync-runtime-health"]) == 0
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked


@pytest.mark.parametrize(
    "message",
    (
        "not-json",
        "[]",
        json.dumps({"playlist": "All media", "source": "schedule", "taboo_entries": {}}),
        json.dumps(
            {
                "playlist": "All media",
                "source": "schedule",
                "taboo_entries": [{"playlist_id": "all-media"}],
            }
        ),
    ),
)
def test_malformed_status_fails_before_any_authoring_write(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "send_runtime", lambda _verb: Response.success(message))

    assert cli.main(["--sync-runtime-health"]) == 1
    assert not pairings.state_path().exists()
    assert not paths.runtime_config_path().exists()


def test_compile_failure_preserves_last_known_good_runtime_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = 'schema_version = 3\nlast_known_good = "keep me"\n'
    target.write_text(previous, encoding="utf-8")
    calls: list[str] = []

    def send(verb: str) -> Response:
        calls.append(verb)
        return _snapshot(item)

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(
        runtime_config,
        "update",
        lambda *_args: (_ for _ in ()).throw(
            runtime_config.RuntimeConfigError("injected compiler failure")
        ),
    )

    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status"]
    assert target.read_text(encoding="utf-8") == previous
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
