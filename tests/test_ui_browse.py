"""The browse dialog's widgets, in as much isolation as GTK allows.

Carries the `gui` marker like `test_ui_grid`: these build real widgets, which
needs a display the Nix check sandbox does not have. Nothing is presented,
nothing is drawn, and no network call is made -- the colour picker is a pure
function of clicks, which is exactly why it is worth pinning here rather than
discovering by hand.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from wall_in_one.providers import wallhaven  # noqa: E402
from wall_in_one.ui import browse_dialog  # noqa: E402


@pytest.fixture(autouse=True)
def _initialised() -> None:
    Adw.init()


def test_the_picker_offers_every_colour_wallhaven_accepts() -> None:
    """A swatch the API would refuse is a search that fails on click."""
    picker = browse_dialog._ColourPicker()
    assert set(picker._buttons) == wallhaven.COLORS


def test_the_palette_keeps_its_order() -> None:
    """Built from a sequence, because a set would reshuffle between runs."""
    assert tuple(browse_dialog._ColourPicker()._buttons) == wallhaven.COLOR_ORDER


def test_nothing_is_selected_to_begin_with() -> None:
    assert browse_dialog._ColourPicker().colour() == ""


def test_choosing_a_colour_reads_back_as_that_colour() -> None:
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    assert picker.colour() == "0066cc"


def test_choosing_a_second_colour_replaces_the_first() -> None:
    """Wallhaven takes one colour, so two selected swatches cannot be shown."""
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    picker._buttons["cc0000"].set_active(True)

    assert picker.colour() == "cc0000"
    assert not picker._buttons["0066cc"].get_active()


def test_clicking_the_selected_colour_clears_it() -> None:
    """How "any colour" is said, without a Clear button that is dead most of
    the time."""
    picker = browse_dialog._ColourPicker()
    picker._buttons["0066cc"].set_active(True)
    picker._buttons["0066cc"].set_active(False)

    assert picker.colour() == ""


def test_a_swatch_is_the_colour_it_claims() -> None:
    texture = browse_dialog._swatch_texture("ff6600", size=4)
    assert (texture.get_width(), texture.get_height()) == (4, 4)


def test_a_default_is_found_by_name_not_position() -> None:
    assert browse_dialog._index_of(browse_dialog.TOP_RANGES, "1M") == 3
    assert browse_dialog.TOP_RANGES[3][0] == "1M"


def test_an_unknown_default_falls_back_to_the_first_entry() -> None:
    assert browse_dialog._index_of(browse_dialog.TOP_RANGES, "nonsense") == 0
