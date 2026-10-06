"""Text people, files and providers wrote is shown exactly as written.

libadwaita reads a row's title and subtitle, a banner's, a toast's, and a
preferences group's title and description, a status page's or a page's
description, as Pango markup. Text with ``&``, ``<`` or ``>`` in it isn't
markup: GTK warned "Failed to set text ... from markup" and showed nothing (a
rule called "Frog & toad", a folder, a provider URL in an error, Tidy's
"<date>"). Rows and banners are built plain (`plain`), toasts too
(`plain_toast`), and the widgets with no plain switch get escaped text
(`markup_text`). The suite's conftest fails any test during which GTK says
"from markup" (`MARKUP_FAILURE`); these check what is shown.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.shown_text import shown_texts  # noqa: E402
from tests.test_ui_next_window import toolkit  # noqa: E402,F401
from wall_in_one.ui.plain_text import markup_text, plain, plain_toast  # noqa: E402

WRITTEN = "Frog & <toad> “big” \"quoted\" 'single' > less"
UI = Path(__file__).parents[1] / "src" / "wall_in_one" / "ui"


@pytest.mark.parametrize(
    "row_type",
    [Adw.ActionRow, Adw.ExpanderRow, Adw.SwitchRow, Adw.ComboRow, Adw.SpinRow],
    ids=lambda row_type: row_type.__name__,
)
def test_a_row_shows_its_title_and_subtitle_as_written(row_type: type[Adw.ActionRow]) -> None:
    row = plain(row_type, title=WRITTEN, subtitle=f"in {WRITTEN}")
    assert not row.get_use_markup()
    shown = shown_texts(row)
    assert WRITTEN in shown and f"in {WRITTEN}" in shown, shown


def test_a_banner_and_a_toast_show_their_title_as_written() -> None:
    banner = plain(Adw.Banner, title=WRITTEN, revealed=True)
    assert WRITTEN in shown_texts(banner)
    toast = plain_toast(WRITTEN)
    assert toast.get_title() == WRITTEN and not toast.get_use_markup()


def test_markup_only_widgets_show_escaped_text_as_written() -> None:
    group = Adw.PreferencesGroup(title=markup_text(WRITTEN), description=markup_text(WRITTEN))
    status = Adw.StatusPage(description=markup_text(WRITTEN))
    label = Gtk.Label()
    label.set_markup(markup_text(WRITTEN))
    assert shown_texts(group).count(WRITTEN) == 2
    assert WRITTEN in shown_texts(status)
    assert label.get_text() == WRITTEN


_ROWS = {"ActionRow", "ExpanderRow", "SwitchRow", "ComboRow", "SpinRow", "EntryRow"}
_ROWS |= {"PasswordEntryRow", "ButtonRow", "Banner"}


def test_every_row_banner_and_toast_in_both_windows_is_plain_text() -> None:
    """The audit, kept: nothing builds one that reads markup again."""
    found: list[str] = []
    for path in sorted(UI.rglob("*.py")):
        if path.name == "plain_text.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            called = ast.unparse(node.func)
            where = f"{path.relative_to(UI)}:{node.lineno}"
            if called.startswith("Adw.") and called[4:] in _ROWS:
                found.append(f"{where}: {called}(...) reads markup; use plain(...)")
            elif called == "Adw.Toast.new":
                found.append(f"{where}: Adw.Toast.new reads markup; use plain_toast(...)")
            elif called == "Adw.Toast" and not any(
                k.arg == "use_markup"
                and isinstance(k.value, ast.Constant)
                and k.value.value is False
                for k in node.keywords
            ):
                found.append(f"{where}: Adw.Toast(...) without use_markup=False")
    assert found == [], "\n".join(found)
