"""One task, one message: what it saved, durability included, and what it refused.

A multi-step authoring task can publish one file whose folder sync then
fails (saved, durability unconfirmed) and then stop at a real refusal in a
later step. The window used to get two messages for that one task: the
caller's own result ("... was deleted, but cleanup needs attention: ...")
and, separately, the lane's "... was saved, but syncing its folder failed".
The authoring lane now folds its task's warnings into the message the
caller says for it (`Application._finish_authoring_outcome`), and says them
on their own only when the caller says nothing.
"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    application_lanes,
    sandboxed_runtime,
    settled,
    toolkit,
)
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from wall_in_one.library import displays, playlists, schedules, state_file  # noqa: E402
from wall_in_one.ui import playlists_page  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

SAVED_BUT = "was saved, but syncing its folder failed"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def _sync_fails_for(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Path]:
    real = state_file.fsync_parent
    failed: list[Path] = []

    def sync(path: Path) -> None:
        if Path(path).name == name:
            failed.append(Path(path))
            raise OSError(errno.EIO, "injected post-replace directory sync failure")
        real(path)

    monkeypatch.setattr(state_file, "fsync_parent", sync)
    return failed


def test_a_delete_saved_without_its_sync_then_stopped_in_cleanup_is_said_once(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The playlist delete publishes, its sync fails; the schedule cleanup is refused."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        made = application.session.playlists.create(
            "Evening",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        application.session.schedules.add(made.id, weekdays=("mon",))
        application.playlists_changed()
        yield "the playlist to be published", lambda: settled(application)

        def refused(_store: schedules.Store, _playlist: str) -> bool:
            raise schedules.ScheduleError("local-io", "schedule disk is read-only")

        monkeypatch.setattr(schedules.Store, "forget_playlist", refused)
        failed = _sync_fails_for(monkeypatch, "playlists.json")
        editing = window.state.playlist_editing
        assert editing is not None
        heard = len(said)
        editing.delete_playlist(made.id)

        def about_the_delete() -> list[str]:
            # The rule left behind then stops the next runtime publication,
            # which says so on its own: that is the runtime lane's message.
            return [m for m in said[heard:] if "Evening" in m or "playlists.json" in m]

        yield (
            "the delete to land",
            lambda: not application._authoring_active and bool(about_the_delete()),
        )

        assert failed, "the delete's own publication failed its sync"
        assert playlists.Store.open().get(made.id) is None, "the playlist is gone"
        # Before: two messages, the caller's and the lane's warning on its own.
        assert len(about_the_delete()) == 1, about_the_delete()
        (message,) = about_the_delete()
        assert message.startswith("“Evening” was deleted, but cleanup needs attention:")
        assert "schedule disk is read-only" in message, "what was refused"
        assert f"playlists.json {SAVED_BUT}" in message, "what was saved, as it was saved"
        assert "nothing changed" not in message
        yield "every tail", lambda: not application._authoring_active
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)


def test_the_classic_delete_is_said_once_too(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic Playlists page deletes through the same task and says it the same way."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        session = application.session
        made = session.playlists.create(
            "Evening",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        session.displays.assign("DP-2", made.id)
        application.playlists_changed()
        yield "the playlist to be published", lambda: settled(application)

        def refused(_store: displays.Store, _playlist: str) -> int:
            raise displays.DisplayError("local-io", "display disk is read-only")

        monkeypatch.setattr(displays.Store, "forget_playlist", refused)
        failed = _sync_fails_for(monkeypatch, "playlists.json")
        monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
        classic = playlists_page.PlaylistsPage(application)
        classic.refresh(session)
        heard = len(said)
        classic._delete_confirmed(made.id)
        yield (
            "the delete to land",
            lambda: not application._authoring_active and len(said) > heard,
        )

        assert failed, "the delete's own publication failed its sync"
        new = [m for m in said[heard:] if "Evening" in m or "playlists.json" in m]
        assert len(new) == 1, new
        (message,) = new
        assert message.startswith("Evening was deleted, but cleanup needs attention:"), message
        assert "display disk is read-only" in message
        assert f"playlists.json {SAVED_BUT}" in message
        yield "every tail", lambda: not application._authoring_active
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)


def test_a_clean_delete_is_one_toast_that_says_what_it_could_not_confirm(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing refused, the new window's own result is a toast: the warning joins it."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        toasts: list[str] = []
        window.state.connect("toast", lambda _state, text, _button: toasts.append(text))
        yield first_scan(application, window)
        made = application.session.playlists.create("Evening")
        application.playlists_changed()
        yield "the playlist to be published", lambda: settled(application)
        failed = _sync_fails_for(monkeypatch, "playlists.json")
        editing = window.state.playlist_editing
        assert editing is not None
        heard = len(said)
        editing.delete_playlist(made.id)
        yield "the delete to land", lambda: settled(application) and bool(toasts)

        assert failed
        assert said[heard:] == [], "nothing said beside the toast"
        (toast,) = toasts
        assert toast.startswith("Deleted “Evening”. "), toast
        assert f"playlists.json {SAVED_BUT}" in toast
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
