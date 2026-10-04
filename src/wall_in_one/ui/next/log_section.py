"""Settings → Log: this session's `ActivityLog`, with file paths hidden by default.

A bounded, read-only list, newest last: the time, what said it (Wall-in-One
itself, the wallpaper service, a renderer, display discovery, playback or
power) and its words, with problems marked. "Copy" puts exactly what is shown
on the clipboard. "Hide file paths" is on whenever Wall-in-One starts and is
never saved: it changes what is shown and copied, not the log. It replaces the
folders the app knows (home, the library folders, its XDG folders) and the
login name, wherever they appear (`activity_log.PathHider`).

The log lives in the application for the whole process; this section follows
it only while it is realized, and catches up when it is shown again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, GObject, Gtk, Pango

from wall_in_one.ui.activity_log import ActivityLog, Entry

#: The list's height: enough to read, never the whole page.
LIST_HEIGHT: Final = 260
DESCRIPTION: Final = (
    "What Wall-in-One and the wallpaper service reported since Wall-in-One started. "
    "It is kept in memory only, never written to disk."
)
_SOURCES: Final = {
    "app": "Wall-in-One",
    "service": "Service",
    "renderer": "Renderer",
    "displays": "Displays",
    "playback": "Playback",
    "power": "Power",
}


class LogSection(Adw.PreferencesGroup):
    """The log, its Copy button and its "Hide file paths" switch."""

    def __init__(self, log: ActivityLog, report: Callable[[str], None]) -> None:
        super().__init__(title="Log", description=DESCRIPTION)
        self._log = log
        self._report = report
        self._unsubscribe: Callable[[], None] | None = None
        #: The rows shown, oldest first, each with the label of its words.
        self._rows: list[tuple[Gtk.Widget, Gtk.Label]] = []

        self.copy = Gtk.Button(
            child=Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copy"),
            valign=Gtk.Align.CENTER,
        )
        self.copy.add_css_class("flat")
        self.copy.set_tooltip_text("Copy the log, as shown, to the clipboard")
        self.copy.connect("clicked", lambda _button: self._copy())
        self.set_header_suffix(self.copy)

        self._list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            margin_top=10,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        self.empty = Gtk.Label(label="Nothing yet.", xalign=0)
        self.empty.add_css_class("dim-label")
        self._list.append(self.empty)
        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            max_content_height=LIST_HEIGHT,
            propagate_natural_height=True,
        )
        self._scroller.set_child(self._list)
        holder = Adw.PreferencesRow(activatable=False, focusable=False)
        holder.set_title("Log")
        holder.set_child(self._scroller)
        self.add(holder)

        self.hiding = Adw.SwitchRow(
            title="Hide file paths",
            subtitle="In the log above and in what Copy copies; on each time Wall-in-One starts",
            active=True,
        )
        self.hiding.connect("notify::active", lambda _row, _param: self._render())
        self.add(self.hiding)

        self.connect("realize", lambda _widget: self.listen())
        self.connect("unrealize", lambda _widget: self.close())

    # -- following the log --------------------------------------------------------------
    def listen(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._log.subscribe(self._added)
            self._render()

    def close(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    @property
    def shown_lines(self) -> list[str]:
        """Each shown entry's words, as shown, oldest first."""
        return [words.get_label() or "" for _row, words in self._rows]

    def text(self) -> str:
        """What Copy copies: every entry, paths hidden while the switch is on."""
        return self._log.as_text(hide=self.hiding.get_active())

    # -- drawing -----------------------------------------------------------------------
    def _added(self, entry: Entry) -> None:
        if self._rows and len(self._rows) >= len(self._log.entries):
            # The log is full and dropped its oldest entry: so does the list.
            oldest, _words = self._rows.pop(0)
            self._list.remove(oldest)
        self._append(entry)

    def _render(self) -> None:
        for row, _words in self._rows:
            self._list.remove(row)
        self._rows = []
        for entry in self._log.entries:
            self._append(entry)
        self.empty.set_visible(not self._rows)
        self._describe()

    def _append(self, entry: Entry) -> None:
        hide = self.hiding.get_active()
        row = Gtk.Box(spacing=10)
        when = Gtk.Label(label=f"{entry.time:%H:%M:%S}", xalign=0, valign=Gtk.Align.START)
        when.add_css_class("dim-label")
        when.add_css_class("numeric")
        source = Gtk.Label(
            label=_SOURCES.get(entry.source, entry.source),
            xalign=0,
            width_chars=11,
            valign=Gtk.Align.START,
        )
        source.add_css_class("heading" if entry.problem else "dim-label")
        words = Gtk.Label(
            label=self._log.shown(entry, hide=hide),
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            hexpand=True,
            selectable=True,
            valign=Gtk.Align.START,
            use_markup=False,
        )
        words.set_focusable(False)
        for label in (when, source, words):
            label.add_css_class("caption")
            label.add_css_class("monospace")
        row.append(when)
        row.append(source)
        row.append(words)
        if entry.problem:
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.set_valign(Gtk.Align.START)
            icon.add_css_class("error" if entry.level == "error" else "warning")
            icon.set_tooltip_text("Problem" if entry.level == "error" else "Warning")
            row.append(icon)
        self._list.append(row)
        self._rows.append((row, words))
        self.empty.set_visible(False)
        self._describe()

    def _describe(self) -> None:
        shown, problems = len(self._rows), self._log.problems
        if not shown:
            self.set_description(DESCRIPTION)
            return
        count = f"{shown} entr{'y' if shown == 1 else 'ies'}"
        self.set_description(
            f"{DESCRIPTION} This session: {count}"
            + (f", {problems} problem{'' if problems == 1 else 's'}." if problems else ".")
        )

    def _copy(self) -> None:
        text = self.text()
        if not text:
            self._report("The log is empty; nothing was copied")
            return
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(text)
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value))
        lines = text.count("\n") + 1
        hidden = " with file paths hidden" if self.hiding.get_active() else ""
        self._report(f"Copied {lines} line{'' if lines == 1 else 's'}{hidden}")
