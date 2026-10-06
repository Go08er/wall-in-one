"""The window never says "nothing changed" for a damaged file it moved aside (Y-4).

Creating a playlist while ``playlists.json`` holds damaged bytes moves the
original aside to ``playlists.json.broken`` and then writes the new file.
Here the state folder is read-only by then, so the new file isn't written.
The window said "“New” was not created; nothing changed: ...", though the
original had moved. It now says the playlist wasn't created, where the
original is kept, and that the new file wasn't saved.

Driven through the real application and the new window's own create, with
real faults on its disposable profile.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_store_failed_replacement import DAMAGED, read_only_after_the_move  # noqa: E402
from tests.test_ui_next_editing import first_scan, reports_of  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
    toolkit,
)
from wall_in_one import paths  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def test_a_create_that_moved_the_damaged_list_aside_says_where_it_is(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        target = paths.app_state_dir() / "playlists.json"
        target.write_bytes(DAMAGED)
        backup = target.with_name("playlists.json.broken")
        editing = window.state.playlist_editing
        assert editing is not None
        heard = len(said)
        with read_only_after_the_move(monkeypatch) as folders:
            editing.create_playlist("New")
            yield "the create to be refused", lambda: len(said) > heard
            yield "the lane to be idle", lambda: not application._authoring_active

        assert folders == [target.parent], "the real move aside happened"
        assert backup.read_bytes() == DAMAGED and not target.exists()
        (message,) = said[heard:]
        # Before: "“New” was not created; nothing changed: local-io: could not write ..."
        assert "nothing changed" not in message, message
        assert message.startswith("“New” was not created:"), message
        assert f"kept as {backup}" in message, message
        assert "its replacement was not saved" in message, message
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
