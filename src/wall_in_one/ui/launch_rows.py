"""The Settings rows that choose how the next start opens, in either window.

Two rows: the **Interface** (classic or the new one) and **GPU acceleration**
for the window (off starts it with GTK's software renderer, ``cairo``).

Both windows offer them -- classic Settings → Appearance, and the new
interface's Settings page -- over the same ``ui.toml`` keeper
(`wall_in_one.ui.next.prefs.UiPrefsKeeper`), ui.toml's one writer.

A row shows what ui.toml holds, not what was last clicked: the keeper
reconciles after every save, success or failure, and the rows follow it. A
choice that could not be saved therefore goes back, with the reason
reported, and choosing it again saves again. While ui.toml can't be written
(a newer version, unreadable or malformed) the rows are off and say why.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one.ui.next.prefs import (
    GPU_NOTE,
    INTERFACE_CHOICES,
    INTERFACE_NOTE,
    UiPrefsKeeper,
)


class LaunchRows:
    """The next start's interface and GPU acceleration, bound to a ui.toml keeper."""

    def __init__(self, keeper: UiPrefsKeeper, report: Callable[[str], None]) -> None:
        self._keeper = keeper
        self._report = report
        #: Set while the rows are being shown what is durable, not edited.
        self._showing = False
        self.interface = Adw.ComboRow(
            title="Interface",
            subtitle=INTERFACE_NOTE,
            model=Gtk.StringList.new([label for _key, label in INTERFACE_CHOICES]),
        )
        self.interface.connect("notify::selected", self._on_interface)
        self.gpu = Adw.SwitchRow(title="GPU acceleration", subtitle=GPU_NOTE)
        self.gpu.connect("notify::active", self._on_gpu)
        self._notes: tuple[tuple[Adw.ActionRow, str], ...] = (
            (self.interface, INTERFACE_NOTE),
            (self.gpu, GPU_NOTE),
        )
        self._unsubscribe = keeper.subscribe(self.show)
        self.show()

    @property
    def rows(self) -> tuple[Adw.ActionRow, ...]:
        """The rows, in the order a group lists them."""
        return tuple(row for row, _note in self._notes)

    def show(self) -> None:
        """Show what the keeper holds, and whether it may be changed."""
        prefs = self._keeper.prefs
        keys = [key for key, _label in INTERFACE_CHOICES]
        self._showing = True
        try:
            self.interface.set_selected(keys.index(prefs.interface))
            self.gpu.set_active(prefs.gpu_acceleration)
        finally:
            self._showing = False
        blocked = self._keeper.read_only
        for row, note in self._notes:
            row.set_subtitle(blocked or note)
            row.set_sensitive(not blocked)

    def close(self) -> None:
        """Stop following the keeper; the window is going away."""
        self._unsubscribe()

    def _change(self, **fields: Any) -> None:
        if not self._keeper.change(**fields):
            self._report(self._keeper.read_only)
            self.show()  # back to what ui.toml holds

    def _on_interface(self, row: Adw.ComboRow, _property: object) -> None:
        index = row.get_selected()
        if not self._showing and 0 <= index < len(INTERFACE_CHOICES):
            self._change(interface=INTERFACE_CHOICES[index][0])

    def _on_gpu(self, row: Adw.SwitchRow, _property: object) -> None:
        if not self._showing:
            self._change(gpu_acceleration=row.get_active())
