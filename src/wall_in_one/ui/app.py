"""Application object: wires the palette, the stylesheet, and the control socket."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import config, paths
from wall_in_one.control import server
from wall_in_one.control.protocol import Response
from wall_in_one.session import Session
from wall_in_one.theme import css, source
from wall_in_one.ui.window import MainWindow
from wall_in_one.wallpaper.applier import Applied, ApplyError


class Application(Adw.Application):
    """Owns app-wide state: settings, the live palette, and the CSS provider."""

    def __init__(self) -> None:
        super().__init__(
            application_id=paths.APPLICATION_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._settings = config.load()
        self._window: MainWindow | None = None
        self._provider = Gtk.CssProvider()
        self._control: server.SocketServer | None = None
        self._resolved: source.ResolvedPalette | None = None
        self._session = Session(self._settings)
        self._cycle_source: int = 0

    # -- lifecycle -------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._start_control_socket()

    def do_activate(self) -> None:
        if self._window is None:
            window = MainWindow(self, self._settings)
            window.connect_settings_changed(self._on_settings_changed)
            window.connect("close-request", self._on_close_request)
            self._window = window
        self.reload_palette()
        self.refresh_library()
        assert self._window is not None
        self._window.present()

    def do_shutdown(self) -> None:
        self._stop_cycle()
        self._session.shutdown()
        if self._control is not None:
            self._control.stop()
            self._control = None
        Adw.Application.do_shutdown(self)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        config.save(self._settings)
        return False

    # -- palette ---------------------------------------------------------

    def reload_palette(self) -> source.ResolvedPalette:
        """Resolve and apply the current palette.

        Idempotent by design: Noctalia runs our template's post-hook on every
        successful render, not only when the colours actually changed.
        """
        resolved = source.resolve(scheme=self._settings.preview_scheme)
        self._resolved = resolved
        self._apply_stylesheet(resolved)
        if self._window is not None:
            self._window.show_palette(resolved)
        return resolved

    def _apply_stylesheet(self, resolved: source.ResolvedPalette) -> None:
        stylesheet = css.render(resolved.palette, opacity=self._settings.opacity)
        self._provider.load_from_string(stylesheet)
        # Match the palette's own mode so libadwaita's built-in rules agree
        # with the colours we just handed it. This also settles the argument
        # with GtkSettings:gtk-application-prefer-dark-theme, which Noctalia's
        # own gtk4 template writes into ~/.config/gtk-4.0/settings.ini and
        # which libadwaita warns about on startup; the style manager wins.
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if resolved.palette.mode == "dark"
            else Adw.ColorScheme.FORCE_LIGHT
        )

    def _on_settings_changed(self, settings: config.Settings) -> None:
        self._settings = settings
        self._session.update_settings(settings)
        if self._resolved is not None:
            self._apply_stylesheet(self._resolved)

    # -- library ---------------------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    def refresh_library(self) -> None:
        """Rescan, then line the cursor up with the wallpaper already on screen."""
        self._session.refresh()
        self._session.sync_with_noctalia()
        if self._window is not None:
            self._window.show_library(self._session)

    def apply(self, action: Callable[[], Applied]) -> Response:
        """Run a navigation action and report it, without letting it kill the app."""
        try:
            applied = action()
        except ApplyError as error:
            return Response.failure(str(error))
        if self._window is not None:
            self._window.show_library(self._session)
        return Response.success(applied.describe())

    # -- cycle timer -----------------------------------------------------

    def _stop_cycle(self) -> None:
        if self._cycle_source:
            GLib.source_remove(self._cycle_source)
            self._cycle_source = 0

    def sync_cycle_timer(self) -> None:
        """Start, stop, or re-time the cycle timer to match the settings."""
        self._stop_cycle()
        if not self._settings.cycle_enabled:
            return
        self._cycle_source = GLib.timeout_add_seconds(
            self._settings.cycle_interval, self._on_cycle_tick
        )

    def _on_cycle_tick(self) -> bool:
        # A failure here is nearly always an empty library or a missing file;
        # neither is a reason to stop cycling, so keep the timer alive.
        self.apply(self._session.next)
        return GLib.SOURCE_CONTINUE

    # -- control socket --------------------------------------------------

    def _start_control_socket(self) -> None:
        control = server.SocketServer(_Commands(self))
        try:
            control.start()
        except RuntimeError as error:
            # Losing the control socket costs the plugin's buttons, not the
            # app. Say so and carry on.
            print(f"warning: control socket unavailable: {error}", file=sys.stderr)
            return
        self._control = control

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def update_settings(self, **changes: Any) -> config.Settings:
        self._settings = replace(self._settings, **changes).validated()
        config.save(self._settings)
        self._session.update_settings(self._settings)
        self.sync_cycle_timer()
        if self._resolved is not None:
            self._apply_stylesheet(self._resolved)
        return self._settings


class _Commands:
    """Control-socket verb implementations.

    Thin on purpose: each verb is one call into the session plus a sentence
    describing what happened. This is the whole surface the Noctalia plugin
    drives.
    """

    def __init__(self, application: Application) -> None:
        self._app = application

    def next_wallpaper(self) -> Response:
        return self._app.apply(self._app.session.next)

    def previous_wallpaper(self) -> Response:
        return self._app.apply(self._app.session.previous)

    def random_wallpaper(self) -> Response:
        return self._app.apply(self._app.session.random)

    def set_shuffle(self, value: str | None) -> Response:
        current = self._app.settings.shuffle
        updated = self._app.update_settings(shuffle=server.parse_toggle(value, current))
        return Response.success(f"shuffle {'on' if updated.shuffle else 'off'}")

    def set_cycle(self, value: str | None) -> Response:
        current = self._app.settings.cycle_enabled
        updated = self._app.update_settings(cycle_enabled=server.parse_toggle(value, current))
        return Response.success(f"cycle {'on' if updated.cycle_enabled else 'off'}")

    def set_cycle_interval(self, value: str | None) -> Response:
        if value is None:
            return Response.success(f"cycle-interval {self._app.settings.cycle_interval}")
        try:
            seconds = int(value)
        except ValueError:
            return Response.failure(f"expected a whole number of seconds, got {value!r}")
        updated = self._app.update_settings(cycle_interval=seconds)
        return Response.success(f"cycle-interval {updated.cycle_interval}")

    def set_dynamics(self, value: str | None) -> Response:
        current = self._app.settings.dynamics_enabled
        updated = self._app.update_settings(dynamics_enabled=server.parse_toggle(value, current))
        return Response.success(f"dynamics {'on' if updated.dynamics_enabled else 'off'}")

    def reload_palette(self) -> Response:
        resolved = self._app.reload_palette()
        return Response.success(f"palette reloaded ({resolved.origin.value})")

    def report_status(self) -> Response:
        return Response.success(self._app.session.describe())

    def quit(self) -> Response:
        GLib.idle_add(self._app.quit)
        return Response.success("quitting")


def run(argv: list[str] | None = None) -> int:
    # GtkApplication overwrites prgname with the application id on Wayland, so
    # setting it here would be cosmetic at best and misleading at worst. The
    # Wayland app-id comes from paths.APPLICATION_ID; see docs/niri.md.
    GLib.set_application_name("Wall-in-One")
    application = Application()
    return application.run(argv if argv is not None else [])
