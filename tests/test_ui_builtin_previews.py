"""A built-in palette's swatches are drawn with no Noctalia file or program present.

The palette manager of the new window lists the built-in palettes; each row's
swatches come from `PaletteEntry.colours`, which is filled from the table in
`theme/builtin_schemes.py` (Noctalia keeps those colours in its binary).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1c import Shown, shown, toolkit  # noqa: E402, F401
from wall_in_one.theme import palettes  # noqa: E402
from wall_in_one.theme.palette import Mode  # noqa: E402
from wall_in_one.ui.next import widgets  # noqa: E402


def _descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        found += _descendants(child)
        child = child.get_next_sibling()
    return found


def test_a_builtin_palette_row_draws_its_colours_without_any_noctalia_files(
    shown: Shown,  # noqa: F811
) -> None:
    assert not palettes.custom_directory().exists()
    assert not palettes.community_directory().exists()
    access = shown.opened.page.access
    assert access is not None
    manager = access.palette_manager
    dialog = shown.opened.page.open_palettes()
    assert dialog is not None
    spin_until(lambda: manager.palette_list().phase == "ready", 30, what="the palettes")

    def row_of(title: str) -> Adw.ActionRow | None:
        for widget in _descendants(dialog):
            if isinstance(widget, Adw.ActionRow) and widget.get_title() == title:
                return widget
        return None

    spin_until(lambda: row_of("Nord") is not None, 30, what="the Nord row")
    row = row_of("Nord")
    assert row is not None
    strips = [w for w in _descendants(row) if isinstance(w, widgets.Swatches)]
    assert len(strips) == 1
    colours = strips[0]._colors
    expected = palettes.builtin_colours("Nord")
    assert expected is not None and colours
    mode: Mode = "dark" if Adw.StyleManager.get_default().get_dark() else "light"
    palette = expected.for_mode(mode)
    # The strip is a few of the palette's own colours, surface first; nothing is empty.
    assert len(colours) >= 5
    assert colours[0] == palette.colours["surface"].hex
    assert palette.colours["primary"].hex in colours
    assert row.get_subtitle() == "compiled into Noctalia"
    manager.close_palettes()
