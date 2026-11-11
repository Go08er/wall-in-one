"""The Library's Select pill icon resolves through GTK's icon theme once the style is installed.

The icon ships in the app (`style.ICON_DIR`), not in any system icon theme,
so it is found only because `style.install` adds that folder to the display's
icon-theme search path. Without that line the pill shows GTK's missing-icon
image (review f1-sonnet-b3, N4).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from wall_in_one.ui.next import style  # noqa: E402
from wall_in_one.ui.next.widgets import SELECT_ICON  # noqa: E402


def test_the_select_icon_resolves_after_the_style_is_installed() -> None:
    if not Gtk.init_check():
        pytest.skip("no display")
    display = Gdk.Display.get_default()
    assert display is not None
    style.install(display)
    theme = Gtk.IconTheme.get_for_display(display)
    assert style.ICON_DIR.is_dir()
    assert theme.has_icon(SELECT_ICON)
    icon = theme.lookup_icon(
        SELECT_ICON, None, 16, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_SYMBOLIC
    )
    file = icon.get_file()
    assert file is not None
    assert (file.get_path() or "").startswith(str(style.ICON_DIR))
