"""Application object: wires the palette, the stylesheet, and the control socket."""

from __future__ import annotations

import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import config, paths
from wall_in_one.browse import Browser
from wall_in_one.control import server
from wall_in_one.control.protocol import Response
from wall_in_one.providers import registry
from wall_in_one.providers.base import SearchQuery, WallpaperCandidate
from wall_in_one.session import Session
from wall_in_one.theme import css, source
from wall_in_one.ui.window import MainWindow
from wall_in_one.wallpaper.applier import Applied, ApplyError


def download_root(settings: config.Settings) -> Path | None:
    """Where a download from the control socket lands, or None to let the browser decide.

    The first configured root, because that is the one the user put first --
    which is exactly what `ui.browse_dialog` does with its own `Browser`, and
    the two paths have to agree or the same wallpaper would arrive in different
    directories depending on which of them asked for it. With none configured
    the `Browser` asks `library.scan`, which is the directory being read from
    anyway.
    """
    configured = settings.roots
    return configured[0] if configured else None


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
        self._browse_jobs: ThreadPoolExecutor | None = None

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
            window.connect("close-request", self._on_close_request)
            self._window = window
        self.reload_palette()
        self.refresh_library()
        assert self._window is not None
        self._window.present()

    def do_shutdown(self) -> None:
        self._stop_cycle()
        self._session.shutdown()
        if self._browse_jobs is not None:
            # Not waiting: a download in flight stages its bytes under a
            # temporary name and links them into place at the end, so an
            # interrupted one leaves nothing behind.
            self._browse_jobs.shutdown(wait=False, cancel_futures=True)
            self._browse_jobs = None
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

    @property
    def resolved_palette(self) -> source.ResolvedPalette | None:
        return self._resolved

    def open_palette_browser(self) -> None:
        """Open the palette browser, from wherever asked."""
        if self._window is not None:
            self._window.open_palette_browser()

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
            # Only the highlight moves: rebuilding the grid on every `next`
            # would throw away every loaded thumbnail and flicker.
            self._window.show_current(self._session)
        return Response.success(applied.describe())

    # -- browsing from the control socket --------------------------------

    def browse_off_thread(self, work: Callable[[Browser], Response]) -> server.Deferred:
        """Run ``work`` against a `Browser` on a worker, and answer when it lands.

        The control server answers from the GTK main loop, so running a search
        or a download where it is called would stop the app drawing for as long
        as the website takes -- seconds, or minutes for a video. Nothing about
        the socket makes that necessary: the client is waiting on a reply, not
        on this thread, so the work goes to a worker and the reply is written
        when it comes back through `GLib.idle_add`. That is the arrangement
        `ui.browse_dialog` already uses for the same two calls, and this is the
        second user of it rather than a second design.

        The connection stays open in the meantime, which costs one file
        descriptor and keeps `ctl search` an ordinary blocking command that
        prints its results.
        """

        def start(reply: Callable[[Response], None]) -> None:
            # Built here rather than in the worker: `Settings.roots` is
            # main-thread state, and this is the thread that owns it.
            browser = Browser(root=download_root(self._settings))
            future = self._browse_pool().submit(work, browser)
            future.add_done_callback(lambda done: self._deliver(done, reply))

        return server.Deferred(start=start)

    def _browse_pool(self) -> ThreadPoolExecutor:
        """The workers browsing verbs run on, made on first use.

        One worker, so two `ctl` invocations arriving together queue instead of
        fighting over each provider's request spacing. Made lazily because most
        runs of this app never touch a provider at all.
        """
        if self._browse_jobs is None:
            self._browse_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-browse")
        return self._browse_jobs

    def _deliver(self, future: Future[Response], reply: Callable[[Response], None]) -> None:
        """Carry a worker's answer back to the main thread. Runs off it."""
        try:
            response = future.result()
        except Exception as error:
            # Broad on purpose: an unreachable network raises whatever the
            # transport underneath felt like, and none of it may reach the
            # client as a traceback or take the app down with it. `server.failed`
            # keeps a ProviderError's machine-readable kind.
            response = server.failed(error)

        def deliver() -> bool:
            reply(response)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

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
        previous = self._settings
        self._settings = replace(self._settings, **changes).validated()
        config.save(self._settings)
        self._session.update_settings(self._settings)
        self.sync_cycle_timer()
        if self._resolved is not None:
            self._apply_stylesheet(self._resolved)
        if self._window is not None:
            self._window.apply_settings(self._settings)
            if self._settings.dynamics_enabled != previous.dynamics_enabled:
                # Dynamics changes which wallpapers are playable at all, so the
                # grid has different contents now, not just a different state.
                self._window.show_library(self._session)
            else:
                self._window.show_current(self._session)
        if self._settings.preview_scheme != previous.preview_scheme:
            self.reload_palette()
        return self._settings


class _Commands:
    """Control-socket verb implementations.

    Thin on purpose: each verb is one call into the session plus a sentence
    describing what happened. The first ten are the whole surface the Noctalia
    plugin drives; the browsing three are for a terminal, and are the only ones
    that answer later rather than at once, because they wait on a website.
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

    # -- browsing ---------------------------------------------------------

    def list_providers(self) -> Response:
        """Every provider and what it cannot currently do.

        The one browsing verb that answers on the spot: the registry reads the
        environment and a small key file, and touches no network at all.
        """
        return Response.success(server.render_providers(registry.describe()))

    def search(self, value: str | None) -> server.Outcome:
        name, text = server.parse_search(value)

        def work(browser: Browser) -> Response:
            # One page, the first: the protocol carries a single argument, and
            # spending it on a page number would cost the query the spaces in it.
            result = browser.search(name, SearchQuery(text=text))
            return Response.success(server.render_search(result))

        return self._app.browse_off_thread(work)

    def download(self, value: str | None) -> server.Outcome:
        name, identifier, variant = server.parse_download(value)

        def work(browser: Browser) -> Response:
            # Both providers re-fetch their own detail page from the identifier
            # and take nothing else off the candidate, so a search result is not
            # needed to name one -- which is what makes `search` and `download`
            # usable as two separate commands. Everything about where the bytes
            # land, the directory marker and the sidecar is the provider's, the
            # same code the download button runs.
            provider = browser.provider(name)
            candidate = WallpaperCandidate(
                provider=name,
                identifier=identifier,
                title="",
                kind=provider.media_kind,
                page_url="",
            )
            done = browser.download(candidate, variant=variant)
            # The file is in the library directory but not in the library until
            # something looks again. Back on the main thread to do it, since the
            # scan ends in the grid.
            GLib.idle_add(self._app.refresh_library)
            return Response.success(f"{done.describe()} -> {done.result.path}")

        return self._app.browse_off_thread(work)

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
