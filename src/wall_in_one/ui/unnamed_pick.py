"""A "Playing now" selector item for a pick with no row on the page (r13f N4, N7).

The pick is either one the reply cannot name (``NAMED_PICK`` is for one it names, which
the page just has no row for), or a named one the page does not list.

At the reply ceiling the service leaves out ``stand_in_for``, and no field says which
playlist a pick is. The app's own record of a pick is written only by its own paths,
not by a pick made through the service (``ctl``, the companion), so it can be stale.
The selector then must not claim "Follow schedule" (no pick holds) or the playlist that
stands in. A ``Adw.ComboRow`` cannot be blank (``set_selected`` with the invalid
position leaves it where it was), so the row gets one more, last item that says so,
and loses it again when a real row can be selected. The page's handlers ignore it.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

UNNAMED_PICK = "Your pick (not named by this reply)"
#: For a pick the reply does name, which the page just has no row for.
NAMED_PICK = "Your pick"
#: Set on the row's model while its last item is the placeholder, so a playlist
#: named like it is not taken for it.
_MARK = "_wio_unnamed_pick"


def _strings(row: Adw.ComboRow) -> Gtk.StringList | None:
    model = row.get_model()
    return model if isinstance(model, Gtk.StringList) else None


def is_shown(row: Adw.ComboRow) -> bool:
    """Whether the row's last item is the placeholder."""
    model = _strings(row)
    count = model.get_n_items() if model is not None else 0
    return model is not None and count > 0 and getattr(model, _MARK, False)


def show(row: Adw.ComboRow, label: str = UNNAMED_PICK) -> None:
    """Select the placeholder with ``label``, adding it if the row has none."""
    model = _strings(row)
    if model is None:
        return
    if not is_shown(row):
        model.append(label)
        setattr(model, _MARK, True)
    elif model.get_string(model.get_n_items() - 1) != label:
        model.splice(model.get_n_items() - 1, 1, [label])
    row.set_selected(model.get_n_items() - 1)


def drop(row: Adw.ComboRow) -> None:
    """Take the placeholder out, if the row has it. Select a real row afterwards."""
    model = _strings(row)
    if model is not None and is_shown(row):
        model.remove(model.get_n_items() - 1)
        setattr(model, _MARK, False)
