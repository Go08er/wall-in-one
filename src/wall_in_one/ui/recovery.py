"""Configuration-error UI with no automatic reset, authoring or service launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import paths
from wall_in_one.theme import css, source
from wall_in_one.ui.readonly_palette import ReadOnlyPalette


class RecoveryWindow(Adw.Application):
    def __init__(self, problem: str) -> None:
        super().__init__(application_id=paths.APPLICATION_ID + ".Repair")
        self.problem = problem
        self.retry = False
        self.window: Adw.ApplicationWindow | None = None
        self._message: Gtk.Label | None = None
        self._provider = Gtk.CssProvider()
        self._colours = ReadOnlyPalette(self._apply_colours)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
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
        if self.window is not None:
            self.window.present()
            return
        self.window = Adw.ApplicationWindow(
            application=self,
            title="Wall-in-One — Configuration needs attention",
            default_width=640,
            default_height=420,
        )
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=24,
            margin_end=24,
            margin_top=20,
            margin_bottom=24,
        )
        heading = Gtk.Label(label="Your configuration needs attention", wrap=True)
        heading.add_css_class("title-2")
        content.append(heading)
        content.append(
            Gtk.Label(
                label="Your files were not reset. Repair the problem below, then try again.",
                wrap=True,
                xalign=0,
            )
        )
        self._message = Gtk.Label(
            label=self.problem, wrap=True, selectable=True, xalign=0, yalign=0
        )
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=110)
        scroll.set_child(self._message)
        content.append(scroll)
        buttons = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            column_spacing=12,
            row_spacing=8,
        )
        for label, target in (
            ("Open settings file", paths.settings_path()),
            ("Open app state folder", paths.runtime_config_path().parent),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _button, path=target: self.open_path(path))
            buttons.append(button)
        retry = Gtk.Button(label="Try again")
        retry.add_css_class("suggested-action")
        retry.connect("clicked", lambda _button: self.retry_startup())
        buttons.append(retry)
        content.append(buttons)
        view.set_content(content)
        self.window.set_content(view)
        self.window.present()

    def open_path(self, path: Path) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        # Repair needs write access when the chosen editor uses a portal.
        launcher.set_writable(path == paths.settings_path())
        launcher.launch(self.window, None, self._opened)

    def _opened(self, launcher: Any, result: Any) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if self._message is not None:
                self._message.set_text(
                    f"{self.problem}\n\nCould not open the file: {error.message}"
                )

    def retry_startup(self) -> None:
        self.retry = True
        if self.window is not None:
            self.window.close()
        self.quit()


def run(problem: str) -> bool:
    application = RecoveryWindow(problem)
    application.run([])
    return application.retry
