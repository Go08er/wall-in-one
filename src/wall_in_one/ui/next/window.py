"""The new interface's window: a placeholder that already survives the real app.

`NextWindow` implements every `WindowServices` member, so the application's
status poll, authoring lanes, library scans, palette worker, control socket
and shutdown all run against it exactly as against `MainWindow`. What it
shows is deliberately small: the live runtime status from the application's
`RuntimeStatusModel`, the library size, and a note when something asks for a
page that has not been ported yet. Its runtime ``show_*`` methods are no-ops
because it renders from its own model subscription instead.

The real interface replaces the content here, page by page, from
``prototypes/ui-redesign``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one import config
from wall_in_one.session import Session
from wall_in_one.theme import source
from wall_in_one.ui.next import status_line
from wall_in_one.ui.status_model import RuntimeStatusView, StatusChange

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: Page names a caller may pass to `show_page`, as the user knows them.
_PAGE_TITLES: Final = {
    "browse": "Store",
    "media": "Library",
    "playlists": "Playlists",
    "schedules": "Schedules",
    "settings": "Settings",
}

NOT_PORTED_HINT: Final = "Start Wall-in-One with --ui=classic to use it."


class NextWindow(Adw.ApplicationWindow):
    """Placeholder for the redesigned interface; implements `WindowServices`."""

    def __init__(self, application: Application, settings: config.Settings) -> None:
        super().__init__(application=application)
        self._settings = settings
        self._library_size: int | None = None
        self._library_scanning = False
        self.set_title("Wall-in-One")
        self.set_default_size(900, 600)

        self._status = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._status.add_css_class("title-4")
        self._library = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._library.add_css_class("dim-label")
        self._note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, visible=False)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        details.append(self._status)
        details.append(self._library)
        details.append(self._note)
        page = Adw.StatusPage(
            icon_name="preferences-desktop-wallpaper-symbolic",
            title="The new interface is on its way",
            description=(
                "This preview shows live status only. "
                "Start Wall-in-One with --ui=classic for every page."
            ),
            child=details,
        )
        self._toast = Adw.ToastOverlay(child=page)
        header = Adw.HeaderBar(
            title_widget=Adw.WindowTitle(title="Wall-in-One", subtitle="New interface preview")
        )
        toolbar = Adw.ToolbarView(content=self._toast)
        toolbar.add_top_bar(header)
        self.set_content(toolbar)

        self._model = application.status_model
        self._unsubscribe: Callable[[], None] | None = None
        self._listen()
        self._render_library()
        # GTK 4's Gtk.Window.destroy() drops GTK's own reference and
        # unrealizes; "destroy" fires only at dispose, which the model's
        # reference to this window would postpone forever. Leave the model
        # when the window is torn down, and rejoin if it is ever realized again.
        self.connect("realize", lambda _window: self._listen())
        self.connect("unrealize", lambda _window: self._stop_listening())
        self.connect("destroy", lambda _window: self._stop_listening())

    # -- the status model --------------------------------------------------

    def _listen(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self._model.subscribe(self._on_status)
            self._render_status(self._model.view)

    def _stop_listening(self) -> None:
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _on_status(self, _change: StatusChange, view: RuntimeStatusView) -> None:
        self._render_status(view)

    def _render_status(self, view: RuntimeStatusView) -> None:
        self._status.set_text(status_line.describe(view))

    @property
    def status_text(self) -> str:
        """The live status line as shown."""
        return self._status.get_text()

    # -- lifecycle ---------------------------------------------------------

    def report(self, message: str) -> None:
        """Show one transient message as a toast."""
        self._toast.add_toast(Adw.Toast.new(message))

    def show_page(self, page: str) -> bool:
        """No page is ported yet: say so in the window and answer False."""
        title = _PAGE_TITLES.get(page, page)
        self._note.set_text(f"{title} is not in the new interface yet. {NOT_PORTED_HINT}")
        self._note.set_visible(True)
        return False

    @property
    def note_text(self) -> str:
        """The not-ported note, or empty while no page has been asked for."""
        return self._note.get_text() if self._note.get_visible() else ""

    # -- settings and theme ----------------------------------------------

    def apply_settings(self, settings: config.Settings) -> None:
        """Keep the latest settings snapshot; nothing here edits settings yet."""
        self._settings = settings

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        """Nothing to do: the application already applied the stylesheet display-wide."""

    def open_palette_browser(self) -> None:
        """Palettes are not ported yet; say so instead of opening nothing."""
        self.report(f"Palettes are not in the new interface yet. {NOT_PORTED_HINT}")

    # -- library and authoring -------------------------------------------

    def show_library(self, session: Session) -> None:
        """Show how many wallpapers the newly installed library holds."""
        self._library_size = len(session.library)
        self._render_library()

    def show_library_scanning(self, scanning: bool) -> None:
        """Mark the library line while a scan runs."""
        self._library_scanning = scanning
        self._render_library()

    def _render_library(self) -> None:
        if self._library_size is None:
            text = "Library not loaded yet"
        else:
            count = self._library_size
            text = f"{count} wallpaper{'' if count == 1 else 's'} in the library"
        if self._library_scanning:
            text += " · scanning…"
        self._library.set_text(text)

    @property
    def library_text(self) -> str:
        """The library line as shown."""
        return self._library.get_text()

    def show_current(self, session: Session) -> None:
        """Nothing highlights the current wallpaper yet; the status line says it."""

    def playlists_changed(self, session: Session) -> None:
        """No playlist view yet."""

    def pairing_health_changed(self, session: Session) -> None:
        """No library grid yet to mark unplayable wallpapers in."""

    # -- runtime status: rendered from the model subscription -------------

    def show_runtime_status(self, status: dict[str, object]) -> None:
        """Rendered from the status model; nothing more to do."""

    def show_runtime_unavailable(self) -> None:
        """Rendered from the status model; nothing more to do."""

    def show_runtime_delayed(self) -> None:
        """Rendered from the status model; nothing more to do."""

    def show_runtime_protocol_error(self, message: str) -> None:
        """Rendered from the status model; the classic window toasts, this one does not yet."""

    def set_runtime_busy(self, busy: bool) -> None:
        """Rendered from the status model; nothing more to do."""
