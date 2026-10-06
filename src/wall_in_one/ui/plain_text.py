"""Text people, files and providers wrote, shown exactly as they wrote it.

Several libadwaita widgets read their text as Pango markup: every row's
title and subtitle (`Adw.PreferencesRow:use-markup` is on by default), a
banner, a toast, and a preferences group's title and description, a page's
or a status page's description. Text with ``&``, ``<`` or ``>`` in it (a
rule called "Frog & toad", a folder, a provider's URL in an error) is then
not markup at all: GTK says "Failed to set text ... from markup" and shows
nothing. Rows and banners are built plain (`plain`), toasts too
(`plain_toast`); the widgets that have no such switch are given their text
escaped (`markup_text`).
"""

from __future__ import annotations

from typing import Any, cast

import gi

gi.require_version("Adw", "1")

from gi.repository import Adw, GLib


def markup_text(text: str) -> str:
    """``text`` as markup that shows exactly ``text``, for a widget with no plain-text switch."""
    return GLib.markup_escape_text(text)


def plain_toast(text: str) -> Adw.Toast:
    """A toast showing ``text`` as it is."""
    return Adw.Toast(title=text, use_markup=False)


def plain[Widget: Adw.PreferencesRow | Adw.Banner](
    widget_type: type[Widget],
    *,
    title: str | None = None,
    subtitle: str | None = None,
    **properties: Any,
) -> Widget:
    """A ``widget_type`` row or banner whose title and subtitle are plain text.

    They are set once ``use-markup`` is off: given to the constructor with
    it, libadwaita parses them as markup first, whatever the order, and warns
    (and shows nothing) for text that isn't markup.
    """
    # mypy calls a union-bound type's constructor the union, not ``Widget``.
    widget = cast(Widget, widget_type(use_markup=False, **properties))
    if title is not None:
        widget.set_property("title", title)
    if subtitle is not None:
        widget.set_property("subtitle", subtitle)
    return widget
