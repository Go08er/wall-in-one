"""Codex's 0.3.0 round-1 findings P-1 and P-2, in the real application.

The fix is the application's own: the settings write and a Library removal
check what is saved when they reach the head of the authoring lane. So each
finding has two tests:

* the review's scenario in the running application with the new window:
  `Application.run` over a sandboxed profile (``dawn.png`` and ``dusk.jpg``)
  and a fake runtime, as ``test_ui_next_editing`` does; it holds the
  authoring lane, queues the review's two gestures in the review's order,
  then lets go, and the second write is refused against what the first one
  saved;
* the application's boundary with no page in front of it (a recording
  window, as ``test_ui_runtime`` uses, standing in for either window): the
  write is refused even though no page checked it first, so the classic
  window's pages get the same guarantee.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_editing import (  # noqa: E402
    first_scan,
    hold_the_lane,
    reports_of,
    wallpaper,
)
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
)
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import playlists, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def _lone(tmp_path: Path) -> tuple[str, str]:
    """An unused playlist holding only ``dawn``: (its id, its one entry's id)."""
    store = playlists.Store.open()
    made = store.create("Lone")
    saved = store.add(made.id, Path(wallpaper(tmp_path, "dawn.png")), entry_id="dawn-entry")
    return saved.id, "dawn-entry"


def test_p1_a_default_queued_behind_the_removal_that_empties_it_is_refused(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review: the entry removal emptied P, then Schedule's default saved P anyway."""
    pid, entry = _lone(tmp_path)
    application = Application(ui="next")
    lanes: list[object] = []
    results: list[bool] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        editing = window.state.playlist_editing
        scheduling = window.state.scheduling
        assert isinstance(editing, RealPlaylists) and isinstance(scheduling, RealScheduling)

        gate = hold_the_lane(application)
        editing.remove_entries(pid, [entry])
        scheduling.choose_default_playlist(pid, results.append)
        gate.set()
        yield "both writes to settle", lambda: settled(application) and bool(results)

        assert results == [False], "the default was refused at the head of the lane"
        emptied = playlists.Store.open().get(pid)
        assert emptied is not None and emptied.entries == (), "the removal ran first"
        assert config.load_strict().active_playlist == ""
        assert application.settings.active_playlist == ""
        assert any("nothing changed" in text and "nothing to play" in text for text in said)
        assert builds_whole(application.settings, application.session)  # still buildable
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_p2_a_removal_queued_behind_the_rule_that_uses_its_playlist_is_refused(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review: a rule saved P while the removal of P's only wallpaper waited."""
    pid, entry = _lone(tmp_path)
    dawn = wallpaper(tmp_path, "dawn.png")
    application = Application(ui="next")
    lanes: list[object] = []
    results: list[bool] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        scheduling = window.state.scheduling
        editing = window.state.editing
        assert isinstance(scheduling, RealScheduling) and editing is not None

        gate = hold_the_lane(application)
        scheduling.save_rule("", RuleView("", pid), results.append)
        editing.remove_wallpapers([dawn])
        gate.set()
        yield (
            "the rule and the refused removal",
            lambda: (
                settled(application)
                and bool(results)
                and any("was not removed" in text for text in said)
            ),
        )

        assert results == [True], "the rule saved"
        assert [rule.playlist for rule in schedules.Store.open().rules] == [pid]
        assert Path(dawn).exists(), "nothing was moved to the Trash"
        kept = playlists.Store.open().get(pid)
        assert kept is not None and [e.id for e in kept.entries] == [entry]
        removed = next(text for text in said if "was not removed" in text)
        assert "“Lone”" in removed and "nothing left to play" in removed
        assert builds_whole(application.settings, application.session)  # still buildable
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- the application's own boundary, whichever window asked -----------------------------


def _windowless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Application, FakeWindow, tuple[MediaItem, MediaItem]]:
    """An application with a recording window, a library of two stills and a quiet runtime."""
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    root = tmp_path / "wallpapers"
    root.mkdir()
    found: list[MediaItem] = []
    for name in ("dawn.png", "dusk.png"):
        (root / name).write_bytes(b"image")
        found.append(MediaItem(root / name, Kind.STILL, 5, 1))
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.adopt_library(Library((root,), tuple(found)))
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    return application, window, (found[0], found[1])


def test_p1_the_application_refuses_a_default_with_nothing_to_play_from_any_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No page checks here: the settings write itself refuses, then accepts once it can play."""
    application, _window, (dawn, _dusk) = _windowless(tmp_path, monkeypatch)
    session = application.session
    lone = session.playlists.create("Lone")  # its last wallpaper already removed
    saved: list[config.Settings] = []
    errors: list[str] = []
    try:
        application.update_settings_async(
            active_playlist=lone.id, on_success=saved.append, on_error=errors.append
        )
        spin_until(lambda: bool(saved or errors))
        assert saved == [] and len(errors) == 1
        assert "can\u2019t be used" in errors[0] and "nothing to play" in errors[0]
        assert config.load_strict().active_playlist == ""
        assert application.settings.active_playlist == ""
        assert builds_whole(application.settings, session)  # still buildable

        session.playlists.add(lone.id, dawn.path)
        application.update_settings_async(
            active_playlist=lone.id, on_success=saved.append, on_error=errors.append
        )
        spin_until(lambda: bool(saved) or len(errors) > 1)
        assert len(errors) == 1, errors
        assert config.load_strict().active_playlist == lone.id
    finally:
        _close(application)


def test_p2_the_application_refuses_a_removal_that_empties_a_used_playlist_from_any_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No page checks here: the removal's own preparation refuses before anything moves."""
    application, window, (dawn, _dusk) = _windowless(tmp_path, monkeypatch)
    session = application.session
    lone = session.playlists.create("Lone")
    session.playlists.add(lone.id, dawn.path, entry_id="dawn-entry")
    session.schedules.add(lone.id)
    finished: list[object] = []
    try:
        application.remove_item_async(dawn, trash=True, finish=finished.append)
        spin_until(lambda: any("was not removed" in text for text in window.reports))
        refused = next(text for text in window.reports if "was not removed" in text)
        assert "\u201cLone\u201d (used by 1 schedule rule)" in refused
        assert "nothing left to play" in refused
        assert finished == [], "the removal never started"
        assert dawn.path.exists(), "nothing was moved to the Trash"
        kept = playlists.Store.open().get(lone.id)
        assert kept is not None and [entry.id for entry in kept.entries] == ["dawn-entry"]
        assert not application._removal_active
        assert builds_whole(application.settings, session)  # still buildable
    finally:
        _close(application)
