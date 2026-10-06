"""What a widget actually shows: the text of every label under it.

A widget's ``title`` property holds what it was given; its label shows that
only if the text parsed (as markup, when the widget reads markup). Text that
isn't markup, given to a widget that reads markup, is shown as nothing, so a
test of what people see reads the labels.
"""

from __future__ import annotations

from gi.repository import Gtk


def shown_texts(widget: Gtk.Widget) -> list[str]:
    """The text of every label in ``widget``'s subtree, in tree order."""
    texts: list[str] = []
    pending: list[Gtk.Widget] = [widget]
    while pending:
        current = pending.pop(0)
        if isinstance(current, Gtk.Label):
            texts.append(current.get_text())
        child = current.get_first_child()
        children: list[Gtk.Widget] = []
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        pending[0:0] = children
    return texts
