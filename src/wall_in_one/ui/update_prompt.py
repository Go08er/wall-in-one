"""A read-only launch notice: never open a second authoring process on update."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from wall_in_one import paths
from wall_in_one.theme import css, source
from wall_in_one.ui.readonly_palette import ReadOnlyPalette

TITLE = "Wall-in-One update needs an app restart"
DESCRIPTION = (
    "A different or unverified version of Wall-in-One is still open. "
    "Finish downloads and pending saves, close that app normally, then open "
    "Wall-in-One again. Your library settings are left unchanged. "
    "The wallpaper service is not restarted by this notice."
)


class UpdateNotice(Adw.Application):
    """Separate identity for the notice; it owns no authoring or runtime state."""

    def __init__(self, show_running: Callable[[], None]) -> None:
        super().__init__(application_id=paths.APPLICATION_ID + ".Update")
        self._show_running = show_running
        self._window: Adw.ApplicationWindow | None = None
        self._provider = Gtk.CssProvider()
        self._colours = ReadOnlyPalette(self._apply_colours)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
            )
        self._colours.start()

    def do_shutdown(self) -> None:
        self._colours.close()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._provider)
        Adw.Application.do_shutdown(self)

    def _apply_colours(self, resolved: source.ResolvedPalette, opacity: float) -> None:
        self._provider.load_from_string(css.render(resolved.palette, opacity=opacity))
        self.get_style_manager().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if resolved.palette.mode == "dark"
            else Adw.ColorScheme.FORCE_LIGHT
        )

    def do_activate(self) -> None:
        if self._window is not None:
            self._window.present()
            return
        window = Adw.ApplicationWindow(
            application=self,
            title=TITLE,
            default_width=520,
            default_height=360,
        )
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.StatusPage(
            title="Restart the open app",
            description=DESCRIPTION,
            icon_name="software-update-available-symbolic",
        )
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        close = Gtk.Button(label="Close notice")
        close.connect("clicked", lambda _button: window.close())
        show = Gtk.Button(label="Show running app")
        show.add_css_class("suggested-action")
        show.connect("clicked", self._on_show_running)
        buttons.append(close)
        buttons.append(show)
        page.set_child(buttons)
        view.set_content(page)
        window.set_content(view)
        window.connect("close-request", self._on_close_request)
        self._window = window
        window.present()

    def _on_close_request(self, _window: Adw.ApplicationWindow) -> bool:
        self._window = None
        return False

    def _on_show_running(self, _button: Gtk.Button) -> None:
        # Activating a different generation is only permitted after this
        # explicit choice. It is not evidence that an update has completed.
        self._show_running()
        if self._window is not None:
            self._window.close()


def run(show_running: Callable[[], None]) -> int:
    return UpdateNotice(show_running).run([])
