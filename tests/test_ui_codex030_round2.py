"""Codex 0.3.0 round 2, Q-1, in the real application: still dependencies.

Codex's two reproductions, through the real removal and the real settings
write, over real files and the real scan: deleting the image a video plays
through, and dropping the folder a scene's capture is in. Each was allowed on
8ce588d and left a profile the compiler refuses; the first also published,
before the rescan, a runtime configuration naming the deleted file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import pairing, scan, workshop  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _file(path: Path, body: bytes = b"placeholder bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.fixture
def made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    applications: list[Application] = []

    def make(roots: tuple[Path, ...], steam: Path | None = None) -> tuple[Application, FakeWindow]:
        """A windowless application over ``roots`` (and a Steam library), really scanned."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # the Trash
        application = _application(tmp_path, monkeypatch)
        applications.append(application)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        if steam is not None:
            monkeypatch.setattr(workshop, "steam_roots", lambda *_a, **_k: (steam,))
        settings = replace(
            application.settings, roots=roots, scan_workshop=steam is not None
        ).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        application.session.update_settings(settings, rescan_library=False)
        return application, window

    yield make
    for application in applications:
        _close(application)


def _adopt_a_real_scan(application: Application, steam: Path | None = None) -> Library:
    settings = application.settings
    library = scan.scan(
        settings.roots,
        dict(application.session.pairings.records),
        include_workshop=steam is not None,
        workshop_roots=(steam,) if steam is not None else None,
        adoption_candidates=(),
    )
    application.session.adopt_library(library, reconcile_workshop=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    return library


def _lane_idle(application: Application) -> bool:
    return (
        not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
    )


def _builds(application: Application) -> bool:
    try:
        runtime_config.render(application.settings, application.session)
    except runtime_config.RuntimeConfigError:
        return False
    return True


def test_real_application_allows_removing_a_retained_videos_only_still(
    tmp_path: Path, made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex Q-1 A: the image video V plays through, Trashed through the real removal."""
    root = tmp_path / "wallpapers"
    video = _file(root / "rain.mp4")
    still = _file(root / "forest.png")
    dawn = _file(root / "dawn.png")
    application, window = made((root,))
    session = application.session
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    library = _adopt_a_real_scan(application)
    found = library.find(video)
    assert found is not None and found.paired_still == still, "no still of its own"
    evening = session.playlists.create("Evening")
    session.playlists.add(evening.id, video)
    session.schedules.add(evening.id)
    assert _builds(application)
    forest = session.library.find(still)
    assert forest is not None
    finished: list[RemovalResult] = []

    # Only V in the scheduled playlist: refused before anything moves.
    application.remove_item_async(forest, trash=True, finish=finished.append)
    spin_until(lambda: _lane_idle(application), 5, what="the removal to settle")
    assert still.exists(), "nothing was moved to the Trash"
    assert finished == []
    assert any(
        "forest was not removed: \u201cEvening\u201d (used by 1 schedule rule) would have "
        "nothing left to play" in report
        for report in window.reports
    ), window.reports
    assert _builds(application)

    # With another wallpaper in it, the removal is allowed; the publication that
    # follows at once, before the rescan, names no deleted file.
    _allowed_removal_publishes_no_deleted_still(
        application, monkeypatch, evening.id, video, forest, dawn
    )


def _allowed_removal_publishes_no_deleted_still(
    application: Application,
    monkeypatch: pytest.MonkeyPatch,
    pid: str,
    video: Path,
    forest: MediaItem,
    dawn: Path,
) -> None:
    """Add ``dawn`` to the playlist, remove ``forest``: allowed, and the interim
    publication (the filtered library, before the rescan) names no deleted file."""
    session = application.session
    still = forest.path
    session.playlists.add(pid, dawn)
    rescans: list[bool] = []
    monkeypatch.setattr(application, "refresh_library", lambda: rescans.append(True))
    finished: list[RemovalResult] = []
    done = threading.Event()

    def removed(result: RemovalResult) -> None:
        finished.append(result)
        done.set()

    application.remove_item_async(forest, trash=True, finish=removed)
    spin_until(done.is_set, 30, what="the removal")
    assert finished[-1].committed and not still.exists()
    spin_until(
        lambda: not application._runtime_compile_pending and paths.runtime_config_path().exists(),
        10,
        what="the interim publication",
    )
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert str(still) not in document, "no runtime.toml names the deleted still"
    assert str(dawn) in document
    kept = session.library.find(video)
    assert kept is not None and kept.paired_still is None, "the filtered library knows"
    assert _builds(application)


def test_an_allowed_removal_of_a_videos_still_publishes_no_deleted_file(
    tmp_path: Path, made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex Q-1 A's interim publication on its own: V's playlist has another wallpaper."""
    root = tmp_path / "wallpapers"
    video = _file(root / "rain.mp4")
    still = _file(root / "forest.png")
    dawn = _file(root / "dawn.png")
    application, _window = made((root,))
    session = application.session
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    _adopt_a_real_scan(application)
    evening = session.playlists.create("Evening")
    session.playlists.add(evening.id, video)
    session.schedules.add(evening.id)
    forest = session.library.find(still)
    assert forest is not None
    _allowed_removal_publishes_no_deleted_still(
        application, monkeypatch, evening.id, video, forest, dawn
    )


def _scene(steam: Path, identifier: str) -> None:
    directory = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / identifier
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Field", "file": "scene.json"}), encoding="utf-8"
    )
    (directory / "scene.pkg").write_bytes(b"\x00" * 16)


def test_folder_guard_misses_a_retained_scenes_automatic_still(tmp_path: Path, made: Any) -> None:
    """Codex Q-1 B: scene C stays in the Workshop; its capture is under folder A."""
    a, b, steam = tmp_path / "A", tmp_path / "B", tmp_path / "steam"
    _file(b / "dawn.png")
    capture = _file(pairing.still_directory(a) / "123.png")
    _scene(steam, "123")
    application, _window = made((a, b), steam)
    library = _adopt_a_real_scan(application, steam)
    scene = next(item for item in library.items if item.kind is Kind.SCENE)
    assert scene.paired_still == capture
    session = application.session
    scenes = session.playlists.create("Scenes")
    session.playlists.add(scenes.id, scene.path)
    session.schedules.add(scenes.id)
    assert _builds(application)
    errors: list[str] = []

    assert "\u201cScenes\u201d (used by 1 schedule rule) would have nothing left to play" in (
        runtime_config.settings_refusal({"roots": (b,)}, session)
    )
    application.update_settings_async(roots=(b,), on_error=errors.append)
    spin_until(lambda: _lane_idle(application), 5, what="the folder change to settle")

    assert config.load_strict().roots == (a, b), "nothing was saved"
    assert len(errors) == 1 and "\u201cScenes\u201d" in errors[0]
    assert capture.exists()
    assert _builds(application)
