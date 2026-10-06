"""The classic tile menu says where a damaged original was kept (Z-4).

Choosing a still or colours from a classic tile's menu while ``pairings.json``
holds damaged bytes moves the original aside to ``pairings.json.broken`` and
then writes the new file. Here the state folder is read-only by then (a
native EACCES), so the new file isn't written. The store's refusal says where
the original is kept, but the classic callbacks threw that away and toasted
only "The colours for dawn could not be saved". Now the toast carries the
store's words. Reset refuses damaged content before moving anything, so its
"nothing changed" stays true, and it now says why too.

Driven through the real application, its classic window and the real
authoring lane, on a disposable profile.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from tests.test_store_failed_replacement import DAMAGED, read_only_after_the_move  # noqa: E402
from tests.test_ui_next_slice import run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    application_lanes,
    sandboxed_runtime,
    settled,
    toolkit,
)
from wall_in_one import paths  # noqa: E402
from wall_in_one.library import pairings  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402

#: The action a tile menu item activates (its handlers don't look at it).
MENU = Gio.SimpleAction.new("tile-menu", None)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def _toasts(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every toast the classic window shows, as shown (after `with_task_durability`)."""
    said: list[str] = []
    added = window._toast.add_toast

    def add_toast(toast: Adw.Toast) -> None:
        said.append(toast.get_title() or "")
        added(toast)

    monkeypatch.setattr(window._toast, "add_toast", add_toast)
    return said


def test_classic_menu_failures_say_where_the_damaged_original_was_kept(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="classic")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        said = _toasts(window, monkeypatch)
        yield (
            "the first scan",
            lambda: settled(application) and len(application.session.library.items) == 2,
        )
        item = application.session.library.items[0]
        target = paths.app_state_dir() / "pairings.json"
        backup = target.with_name("pairings.json.broken")
        policy = pairings.PalettePolicy(kind="community", name="Nord")

        # -- colours, through the tile menu's own action -----------------------------
        target.write_bytes(DAMAGED)
        heard = len(said)
        with read_only_after_the_move(monkeypatch) as folders:
            window._on_palette_path(MENU, GLib.Variant("(ss)", (str(item.path), policy.encode())))
            yield "the colours to be refused", lambda: len(said) > heard
            yield "the lane to be idle", lambda: not application._authoring_active
        assert folders == [target.parent], "the real move aside happened"
        assert backup.read_bytes() == DAMAGED and not target.exists()
        (message,) = said[heard:]
        # Before: "The colours for dawn could not be saved", nothing more.
        assert message.startswith(f"The colours for {item.name} could not be saved:"), message
        assert f"kept as {backup}" in message and "its replacement was not saved" in message
        assert "Permission denied" in message and "nothing changed" not in message

        # -- the still, the same way ---------------------------------------------------
        backup.rename(backup.with_name("pairings.json.broken-colours"))
        target.write_bytes(DAMAGED)
        heard = len(said)
        with read_only_after_the_move(monkeypatch) as folders:
            window._store_still(item, None)
            yield "the still to be refused", lambda: len(said) > heard
            yield "the lane to be idle again", lambda: not application._authoring_active
        assert folders == [target.parent]
        (message,) = said[heard:]
        assert message.startswith(f"The still for {item.name} could not be saved:"), message
        assert f"kept as {backup}" in message and "nothing changed" not in message

        # -- reset: refused before anything moves, so "nothing changed" is true -----
        target.write_bytes(DAMAGED)
        before = sorted(p.name for p in target.parent.iterdir())
        heard = len(said)
        window._on_reset_pairing(MENU, GLib.Variant("s", str(item.path)))
        yield "the reset to be refused", lambda: len(said) > heard
        (message,) = said[heard:]
        assert message.startswith(f"{item.name} could not be reset; nothing changed: "), message
        assert len(message) > len(f"{item.name} could not be reset; nothing changed: ")
        assert sorted(p.name for p in target.parent.iterdir()) == before

        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)
