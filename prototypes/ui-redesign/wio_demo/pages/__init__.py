"""Page contract. The shell owns one header bar and swaps each page's widgets in.

A page module exposes ``create(state) -> Page``. Keep page modules independent:
they talk to each other only through ``state`` (``state.navigate("playlist:frog-day")``,
``state.toast(...)``, ``state.emit_changed(topic)``).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk


class Page:
    #: Stack name; the sidebar uses it as the navigation key.
    name = "page"
    title = "Page"
    subtitle = ""

    def __init__(self, state) -> None:
        self.state = state
        self.widget: Gtk.Widget = Gtk.Box()
        self._title = Adw.WindowTitle(title=self.title, subtitle=self.subtitle)

    # Header contributions. Return the same widget objects every call.
    def header_start(self) -> list[Gtk.Widget]:
        return []

    def header_end(self) -> list[Gtk.Widget]:
        return []

    def title_widget(self) -> Gtk.Widget:
        return self._title

    def set_title(self, title: str, subtitle: str = "") -> None:
        self._title.set_title(title)
        self._title.set_subtitle(subtitle)

    def activate(self, argument: str | None) -> None:
        """Called whenever the page is shown; ``argument`` is the part after ':'."""

    def focus_search(self) -> bool:
        """Ctrl+F. Return True if the page has a search field and focused it."""
        return False

    def demo(self, scene: str) -> None:
        """Screenshot hook: put the page into a named demo state (see scenes.py)."""


class Placeholder(Page):
    def __init__(self, state, name: str, title: str, icon: str) -> None:
        self.name, self.title = name, title
        super().__init__(state)
        status = Adw.StatusPage(icon_name=icon, title=title, description="Coming in this prototype")
        self.widget = status
