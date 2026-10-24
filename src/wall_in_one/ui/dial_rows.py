"""Rows for the renderer dials and a scene's Wallpaper Engine properties, in both interfaces.

One control per dial (`wall_in_one.dials`): a switch, a number in its
range, a choice among its options; per property a switch, a slider's
number, a colour, an option or a line of text. A wallpaper's own row says
"Default (…)" while it follows the default and "Overridden" once it has its
own, with a button that takes it back. Nothing here saves: each row hands
its value to a callback, numbers once they stop changing, so a spin through
a range is one save and not one renderer restart per step.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from wall_in_one import dials
from wall_in_one.ui.plain_text import plain

#: How long a number has to stay put before it is handed on, in milliseconds.
SETTLE_MS: Final = 450

LOGGER = logging.getLogger(__name__)


def _digits(step: float) -> int:
    """Decimals a step needs: 0.05 → 2, 0.5 → 1, 1 → 0."""
    if step <= 0 or step >= 1:
        return 0
    return min(6, max(0, math.ceil(-math.log10(step) - 1e-9)))


#: Every settle timer that is waiting, from any pane (a pane that built again
#: has dropped its own `Settling`, and a close has no pane to ask): token →
#: (the wallpaper it is for, the row's key, its cancel, its flush).
_PENDING: dict[object, tuple[Hashable, str, Callable[[], None], Callable[[], None]]] = {}


#: How many waiting numbers are being handed on right now (`handing_on`).
_HANDING_ON = 0


def handing_on() -> bool:
    """Whether a waiting number is being handed on right now: by its own timer, or a flush.

    The application admits the write it asks for even once a quit was
    asked (`Application._admitted_during_quit`): the person entered the
    number before the quit could end the process. GTK's thread only.
    """
    return _HANDING_ON > 0


@contextmanager
def _handing() -> Iterator[None]:
    global _HANDING_ON
    _HANDING_ON += 1
    try:
        yield
    finally:
        _HANDING_ON -= 1


def cancel_pending(owner: Hashable, key: str | None = None) -> None:
    """Drop the waiting numbers of ``owner`` (one row's, with ``key``), whichever pane made them.

    A reset is the last word for its wallpaper: a number nudged before it must
    not land after it, even one a previous build handed on to an idle. Another
    wallpaper's numbers stay.
    """
    for token, (found, row, cancel, _flush) in tuple(_PENDING.items()):
        if found == owner and (key is None or row == key):
            _PENDING.pop(token, None)
            cancel()


def flush_all() -> int:
    """Hand on every waiting number now, each to the wallpaper it was entered for; how many.

    For a window that is closing, or an application that quits: the process
    may end before a timer fires. The callbacks are the timers' own, so the
    write takes the same path (and lock) as one that waited.

    One that raises is logged, and every other is still handed on: nothing
    is raised to the caller, whose own teardown follows (review r13b, N-f).
    """
    handed = 0
    for token, (_owner, _row, _cancel, flush) in tuple(_PENDING.items()):
        if _PENDING.pop(token, None) is None:
            continue  # an earlier one's hand-on dropped it (a reset): nothing waits
        handed += 1
        try:
            flush()
        except Exception:
            LOGGER.exception("a waiting renderer setting could not be handed on")
    return handed


class Settling:
    """The pending settle timers of one pane's rows, for one wallpaper (``owner``).

    A pane makes one per build. `cancel` is for a reset of its own ("Use
    defaults" must not be written over by a number nudged a moment ago); it
    reaches numbers an earlier build handed on too (`cancel_pending`).
    `flush` is for a pane that builds again: every number still waiting is
    handed on now, to the wallpaper and dial it was entered for (the row's
    callback is bound to those, not to what the pane shows later), so a save
    that rebuilds the pane never loses the number nudged beside it.
    """

    def __init__(self, owner: Hashable = None) -> None:
        self.owner = owner
        self._rows: list[Callable[[], None]] = []

    def add(self, flush: Callable[[], None]) -> None:
        self._rows.append(flush)

    def cancel(self) -> None:
        cancel_pending(self.owner)

    def flush(self) -> None:
        for flush in self._rows:
            flush()

    def flush_soon(self) -> None:
        """`flush`, once the pane that is building has finished (a save may build it again)."""

        def later() -> bool:
            self.flush()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(later)


def _settled(
    callback: Callable[[], None], settling: Settling | None, key: str
) -> tuple[Callable[[], None], Callable[[], None]]:
    """``callback``, run once the calls to it have stopped for `SETTLE_MS`; and its cancel.

    The timer outlives its row (a pane built again, another wallpaper shown,
    a window closed): the number is still the person's, and ``callback`` is
    bound to the wallpaper and dial the row was made for, so it lands there.
    A flush clears the timer before it runs the callback, so it cannot run
    twice or recurse.
    """
    pending: list[int] = []
    token = object()
    owner = settling.owner if settling is not None else None

    def cancel() -> None:
        _PENDING.pop(token, None)
        while pending:
            GLib.source_remove(pending.pop())

    def flush() -> None:
        if pending:
            cancel()
            with _handing():
                callback()

    def fire() -> bool:
        pending.clear()
        _PENDING.pop(token, None)
        with _handing():
            callback()
        return GLib.SOURCE_REMOVE

    def call() -> None:
        if pending:
            GLib.source_remove(pending.pop())
        pending.append(GLib.timeout_add(SETTLE_MS, fire))
        _PENDING[token] = (owner, key, cancel, flush)

    if settling is not None:
        settling.add(flush)
    return call, cancel


def setting_row(dial: dials.Dial) -> Adw.PreferencesRow:
    """A Settings row for ``dial``'s default; `row_value` and `show_value` read and set it."""
    if dial.kind == "bool":
        return plain(Adw.SwitchRow, title=dial.label, subtitle=dial.help)
    if dial.kind in ("int", "float"):
        adjustment = Gtk.Adjustment(
            lower=dial.minimum,
            upper=dial.maximum,
            step_increment=dial.step,
            page_increment=dial.step * 10,
            value=float(dial.default),
        )
        return plain(
            Adw.SpinRow,
            title=dial.label,
            subtitle=dial.help,
            adjustment=adjustment,
            digits=_digits(dial.step) if dial.kind == "float" else 0,
        )
    return plain(
        Adw.ComboRow,
        title=dial.label,
        subtitle=dial.help,
        model=Gtk.StringList.new([label for _value, label in dial.choices]),
    )


def row_value(dial: dials.Dial, row: Adw.PreferencesRow) -> dials.Value:
    """What ``row`` (made by `setting_row`) shows, as ``dial``'s type; the default if nothing."""
    if isinstance(row, Adw.SwitchRow):
        return row.get_active()
    if isinstance(row, Adw.SpinRow):
        number = row.get_value()
        return round(number, 2) if dial.kind == "float" else round(number)
    if isinstance(row, Adw.ComboRow):
        index = row.get_selected()
        if 0 <= index < len(dial.choices):
            return dial.choices[index][0]
    return dial.default


def show_value(dial: dials.Dial, row: Adw.PreferencesRow, value: dials.Value) -> None:
    """Put ``value`` in ``row`` without anything else: the caller holds its own loading flag."""
    if isinstance(row, Adw.SwitchRow):
        row.set_active(bool(value))
    elif isinstance(row, Adw.SpinRow):
        row.set_value(float(value))
    elif isinstance(row, Adw.ComboRow):
        keys = [choice for choice, _label in dial.choices]
        if value in keys:
            row.set_selected(keys.index(value))


def setting_signal(row: Adw.PreferencesRow) -> str:
    """The notification a `setting_row` sends when a person changes it."""
    if isinstance(row, Adw.SwitchRow):
        return "notify::active"
    if isinstance(row, Adw.SpinRow):
        return "notify::value"
    return "notify::selected"


def _reset_button(own: bool, back: Callable[[], None], what: str) -> Gtk.Button:
    button = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.set_tooltip_text(f"Use the default for {what}")
    button.update_property([Gtk.AccessibleProperty.LABEL], [f"Reset {what} to the default"])
    button.set_visible(own)
    button.connect("clicked", lambda _button: back())
    return button


def own_subtitle(own: bool, default: str) -> str:
    """“Default (…)”, or “Overridden” with the default it left."""
    return f"Overridden · default {default}" if own else f"Default ({default})"


def own_row(
    view: dials.DialView,
    on_change: Callable[[dials.Value | None], None],
    *,
    locked: str = "",
    settling: Settling | None = None,
) -> Adw.PreferencesRow:
    """A wallpaper's own row for one dial. ``on_change(None)``: back to the default.

    ``locked``: why nothing can change now; the row is shown, greyed out, saying so.
    """
    dial = view.dial
    row = setting_row(dial)
    row.set_name(f"dial:{dial.renderer}:{dial.key}")
    if isinstance(row, Adw.ActionRow):
        row.set_subtitle(own_subtitle(view.own, dial.label_of(view.default)))
    show_value(dial, row, view.value)
    cancel_settling: list[Callable[[], None]] = []

    def back() -> None:
        for cancel in cancel_settling:
            cancel()
        if settling is not None:  # and one an earlier build of the pane handed on
            cancel_pending(settling.owner, row.get_name())
        on_change(None)

    reset = _reset_button(view.own, back, dial.label.lower())
    if isinstance(row, Adw.ActionRow):
        row.add_suffix(reset)

    def hand_on() -> None:
        value = row_value(dial, row)
        if value != view.value or not view.own:
            on_change(value)

    handler = hand_on
    if dial.kind in ("int", "float"):
        handler, cancel = _settled(hand_on, settling, row.get_name())
        cancel_settling.append(cancel)
    row.connect(setting_signal(row), lambda *_arguments: handler())
    row.set_sensitive(view.available and not locked)
    if locked:
        row.set_tooltip_text(locked)
    elif not view.available:
        row.set_tooltip_text("Does nothing while it is silent")
    return row


def _rgba(text: str) -> Gdk.RGBA:
    colour = Gdk.RGBA()
    parts = [float(part) for part in text.split()]
    colour.red, colour.green, colour.blue = parts[:3]
    colour.alpha = 1.0
    return colour


def _colour_text(colour: Gdk.RGBA) -> str:
    return " ".join(
        f"{min(1.0, max(0.0, channel)):.6f}" for channel in (colour.red, colour.green, colour.blue)
    )


def property_row(
    view: dials.PropertyView,
    on_change: Callable[[str | None], None],
    *,
    locked: str = "",
    settling: Settling | None = None,
) -> Adw.PreferencesRow:
    """A scene's row for one Wallpaper Engine property, as the engine reads its value."""
    setting = view.setting
    cancel_settling: list[Callable[[], None]] = []

    def back() -> None:
        for cancel in cancel_settling:
            cancel()
        if settling is not None:
            cancel_pending(settling.owner, f"property:{setting.name}")
        on_change(None)

    reset = _reset_button(view.own, back, setting.label)
    default_text = view.default
    row: Adw.PreferencesRow
    if setting.type == "bool":
        switch = plain(Adw.SwitchRow, title=setting.label, active=view.value == "true")
        switch.connect(
            "notify::active",
            lambda r, _p: on_change("true" if r.get_active() else "false"),
        )
        default_text = "On" if view.default == "true" else "Off"
        row = switch
    elif setting.type == "slider":
        digits = _digits(setting.step)
        spin = plain(
            Adw.SpinRow,
            title=setting.label,
            adjustment=Gtk.Adjustment(
                lower=setting.minimum,
                upper=setting.maximum,
                step_increment=setting.step,
                page_increment=setting.step * 10,
                value=float(view.value),
            ),
            digits=digits,
        )
        settled, cancel = _settled(
            lambda: on_change(repr(round(spin.get_value(), max(digits, 2)))),
            settling,
            f"property:{setting.name}",
        )
        cancel_settling.append(cancel)
        spin.connect("notify::value", lambda *_arguments: settled())
        row = spin
    elif setting.type == "color":
        action = plain(Adw.ActionRow, title=setting.label)
        button = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER
        )
        button.set_rgba(_rgba(view.value))
        button.connect("notify::rgba", lambda b, _p: on_change(_colour_text(b.get_rgba())))
        action.add_suffix(button)
        row = action
    elif setting.type == "combo":
        values = [value for value, _label in setting.options]
        combo = plain(
            Adw.ComboRow,
            title=setting.label,
            model=Gtk.StringList.new([label for _value, label in setting.options]),
        )
        if view.value in values:
            combo.set_selected(values.index(view.value))
        combo.connect(
            "notify::selected",
            lambda r, _p: (
                on_change(values[r.get_selected()]) if 0 <= r.get_selected() < len(values) else None
            ),
        )
        default_text = dict(setting.options).get(view.default, view.default)
        row = combo
    else:
        entry = plain(Adw.EntryRow, title=setting.label, show_apply_button=True)
        entry.set_text(view.value)

        def apply(editable: Adw.EntryRow) -> None:
            text = editable.get_text()
            if dials.valid_text(text):
                editable.remove_css_class("error")
                on_change(text)
            else:
                editable.add_css_class("error")

        entry.connect("apply", apply)
        row = entry
    row.set_name(f"property:{setting.name}")
    if isinstance(row, Adw.ActionRow):
        row.set_subtitle(own_subtitle(view.own, default_text))
    elif isinstance(row, Adw.EntryRow):
        row.set_tooltip_text(own_subtitle(view.own, default_text))
    if isinstance(row, Adw.ActionRow | Adw.EntryRow):
        row.add_suffix(reset)
    row.set_sensitive(not locked)
    if locked:
        row.set_tooltip_text(locked)
    return row
