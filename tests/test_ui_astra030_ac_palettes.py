"""The palette editors save over the file they opened, and over no other.

An editor holds the whole palette document from when it opened, and saving
rewrites the file from it (`palettes.save_edits`). Whatever another program
(Noctalia, a text editor, the other window) wrote there since would be undone,
so the file is read again just before it is replaced and anything but the
document that was opened is left alone. Each test runs the real application,
has a second process change the file, and then saves from the editor. Each
fails on 7bdc1a6.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_palettes import _document as _palette_document  # noqa: E402
from tests.test_ui_astra030_ac_saved import _another_program  # noqa: E402
from tests.test_ui_codex030_round1c import Shown, shown, toolkit  # noqa: E402, F401
from wall_in_one.theme import palettes  # noqa: E402
from wall_in_one.ui import palette_browser  # noqa: E402
from wall_in_one.ui.next.settings_state import PaletteDraft  # noqa: E402


def _changed_elsewhere(entry: palettes.PaletteEntry) -> bytes:
    """Another program rewrites the palette's file; what it left there."""
    assert entry.path is not None
    theirs = _palette_document(mPrimary="#abcdef")
    theirs["terminal"] = {"note": "theirs"}
    _another_program(files={str(entry.path): json.dumps(theirs)})
    return entry.path.read_bytes()


def test_the_new_window_does_not_save_a_palette_over_another_programs_change(
    shown: Shown,  # noqa: F811
) -> None:
    """An editor holds the whole document from when it opened, and saving
    rewrites the file from it. What another program wrote there since stays."""
    opened = shown.opened
    mine = palettes.write_custom("Mine", _palette_document(mPrimary="#112233"))
    access = opened.page.access
    assert access is not None
    manager = access.palette_manager
    manager.open_palettes()
    spin_until(
        lambda: any(item.name == "Mine" for item in manager.palette_list().items),
        30,
        what="the palettes",
    )
    key = next(item.key for item in manager.palette_list().items if item.name == "Mine")
    drafts: list[PaletteDraft | None] = []
    manager.palette_draft(key, copy=False, done=drafts.append)
    spin_until(lambda: bool(drafts), 30, what="the editor's document")
    draft = drafts[0]
    assert draft is not None
    theirs = _changed_elsewhere(mine)

    saved: list[bool] = []
    colors = {
        "dark": {**draft.colors["dark"], "primary": "#445566"},
        "light": draft.colors["light"],
    }
    manager.save_palette_draft(draft, "Mine", colors, saved.append)
    opened.wait("the save to settle", lambda: bool(saved))

    # Failed on 7bdc1a6: saved over it, and the other program's change was gone.
    assert saved == [False]
    assert mine.path is not None and mine.path.read_bytes() == theirs
    assert opened.reports[-1] == (
        "The palette was not saved: Mine changed on disk since it was opened, and saving "
        "would undo that change; open it again to edit it as it is now"
    )
    manager.close_palettes()


def test_the_classic_editor_does_not_save_a_palette_over_another_programs_change(
    shown: Shown,  # noqa: F811
) -> None:
    opened = shown.opened
    application = opened.application
    mine = palettes.write_custom("Mine", _palette_document(mPrimary="#112233"))
    assert mine.path is not None
    saved: list[palettes.PaletteEntry] = []
    said: list[str] = []
    editor = palette_browser._PaletteEditor(
        application=application,
        title="Edit Mine",
        name="Mine",
        document=palettes.read_document(mine.path),
        in_place=mine,
        on_saved=saved.append,
        on_error=said.append,
    )
    theirs = _changed_elsewhere(mine)

    editor._on_save(Gtk.Button())
    opened.wait("the save to settle", lambda: bool(saved) or bool(said))

    # Failed on 7bdc1a6: saved over it.
    assert saved == []
    assert mine.path.read_bytes() == theirs
    assert said == [
        "Mine changed on disk since it was opened, and saving would undo that change; "
        "open it again to edit it as it is now"
    ]
    editor.force_close()
