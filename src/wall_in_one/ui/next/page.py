"""The page contract. The shell owns one header bar and swaps each page's widgets in.

A page exposes its body (``widget``), the header widgets it contributes, and
hooks the shell calls when it is shown. Pages never import each other: they
talk through the `AppState` (``state.navigate("playlist:frog-day")``,
``state.toast(...)``, and its ``changed`` topics).
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one.ui.next.catalog import CLASSIC_HINT
from wall_in_one.ui.next.state import AppState


class Page:
    #: Stack name; the sidebar uses it as the navigation key.
    name = "page"
    title = "Page"
    subtitle = ""

    def __init__(self, state: AppState) -> None:
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
        """Screenshot hook of the design prototype: put the page into a named state."""


class Placeholder(Page):
    """A page that is not built yet: an honest status page in its place."""

    def __init__(
        self,
        state: AppState,
        name: str,
        title: str,
        icon: str,
        description: str = "Coming in this prototype",
    ) -> None:
        self.name, self.title = name, title
        super().__init__(state)
        self.status = Adw.StatusPage(icon_name=icon, title=title, description=description)
        self.widget = self.status


def not_ported(name: str, title: str, icon: str) -> Callable[[AppState], Page]:
    """A factory for a page the new interface does not have yet."""

    def create(state: AppState) -> Page:
        return Placeholder(
            state,
            name,
            title,
            icon,
            f"{title} is not in the new interface yet. {CLASSIC_HINT}",
        )

    return create
