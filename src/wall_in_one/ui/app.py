"""Application object: wires the palette, the stylesheet, and the control socket."""

from __future__ import annotations

import contextlib
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
from wall_in_one.library import favourites, pairings
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import MediaItem
from wall_in_one.providers import registry
from wall_in_one.providers.base import SearchQuery, WallpaperCandidate
from wall_in_one.session import Session
from wall_in_one.theme import css, source
from wall_in_one.ui.stills import StillMaker
from wall_in_one.ui.window import ACCELERATORS, MainWindow
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
        self._stills = StillMaker()

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
        self._install_accelerators()
        self._start_control_socket()

    def _install_accelerators(self) -> None:
        """Bind the keys. The table lives with the window, which owns the actions.

        Several accelerators may share one action, so they are collected per
        action before being set -- `set_accels_for_action` replaces the list
        rather than adding to it, and binding them one at a time would leave
        only the last.
        """
        bound: dict[str, list[str]] = {}
        for _section, accelerator, action, _description in ACCELERATORS:
            bound.setdefault(action, []).append(accelerator)
        for action, accelerators in bound.items():
            self.set_accels_for_action(action, accelerators)

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
        self._stills.shutdown()
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
        self._make_missing_stills()

    def _make_missing_stills(self) -> None:
        """Fill in the stills for videos that have none, in the background.

        Without this a video only gets its still at the moment dynamics are
        switched off, and only the one video that was playing. Every other one
        keeps dropping out of the rotation when dynamics are off, and keeps
        leaving Noctalia's palette derived from whatever was on screen before.
        """
        # The root the scan actually read from, rather than `download_root`'s
        # answer: that one is allowed to be None so the Browser can decide for
        # itself, and a still has to go somewhere `pairing` will look, which
        # means somewhere the library is read from.
        roots = self._session.library.roots
        if not roots:
            return
        self._stills.request(self._session.library.items, roots[0], self._on_stills_made)

    def _on_stills_made(self, made: int) -> None:
        """A batch finished, so the pairings it wrote are worth re-reading.

        Safe against looping: `StillMaker` remembers every video it has
        attempted, so this rescan cannot queue the same work again.
        """
        del made
        self.refresh_library()

    def favourites_changed(self) -> None:
        """Bring everything that reads the favourites back into line.

        Two readers, and neither may be left behind by a star toggled over the
        socket. The rotation is narrowed from the same store when
        `cycle_favourites_only` is on, which is `Session.favourites_changed`'s
        job; the window's counts and its favourites view come from a rebuild.
        This is the pair the window's own star button already does.
        """
        self._session.favourites_changed()
        if self._window is not None:
            self._window.show_library(self._session)

    def forget(self, path: Path) -> None:
        """Drop a wallpaper this app has just destroyed, and rescan.

        The star and the pairing are the two pieces of state that outlive the
        file, and an entry for something we deleted ourselves is pointless:
        both survive a missing file because the file might come back, which is
        not true of one we have just unlinked. The store's own write failing changes nothing
        here -- the file is gone either way, and the socket has already been
        told what happened to it.

        The rescan is deferred for the reason a finished download's is: it is
        the window's work, not the client's, and `ctl remove` should not be
        held open while six hundred files are walked to confirm that one of
        them is missing.
        """
        with contextlib.suppress(favourites.FavouritesError):
            self._session.favourites.discard(path)
        with contextlib.suppress(pairings.PairingError):
            self._session.pairings.forget_path(path)
        GLib.idle_add(self.refresh_library)

    def pairing_changed(self, item: MediaItem) -> None:
        """Make the window agree after a pairing moved over the socket.

        Only the wallpaper on screen is re-applied: changing the colours of
        something nobody is looking at would be a surprise. The rescan is
        deferred for the reason every other one here is -- it is the window's
        work, not the client's, and `ctl palette` should not be held open
        while the library is walked.
        """
        session = self._session
        cursor = session.cursor
        if cursor is not None and cursor.path == item.path:
            GLib.idle_add(self._reapply_current)
        GLib.idle_add(self.refresh_library)

    def _reapply_current(self) -> bool:
        self.apply(self._session.apply_current)
        return GLib.SOURCE_REMOVE

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
    describing what happened. The first nine are the whole surface the Noctalia
    plugin drives; the library six and the browsing three are for a terminal.
    The browsing three are the only ones that answer later rather than at once,
    because they wait on a website.

    What is thin here is not the same as easy. Every verb below that takes a
    path hands it to `control.server` to be resolved against the library before
    anything happens to a file, and every one that changes the library or the
    favourites leaves through `Application.forget` or
    `Application.favourites_changed`, so the running window never disagrees
    with what the socket just did.
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

    # -- the library ------------------------------------------------------

    def list_library(self, value: str | None) -> Response:
        """What is in the library, as rows a script can read.

        The scan the window is already showing, not a fresh one: `ctl list`
        answers what the app currently believes, which is the same thing the
        grid is drawing. `ctl status` reports the counts and the refresh button
        re-reads the disk.
        """
        kinds, text = server.parse_list(value)
        session = self._app.session
        return Response.success(
            server.render_library(
                session.library.items,
                library_filter.Query(text=text, kinds=kinds),
                session.favourites.paths,
            )
        )

    def select_wallpaper(self, value: str | None) -> Response:
        item = server.resolve(self._app.session.library, value, verb="select")
        return self._app.apply(lambda: self._app.session.select(item.path))

    def list_favourites(self) -> Response:
        session = self._app.session
        return Response.success(
            server.render_favourites(
                session.favourites.favourites.entries,
                tuple(item.path for item in session.library.items),
            )
        )

    def show_pairing(self, value: str | None) -> Response:
        """What one wallpaper resolves to: still, motion, palette, mode."""
        session = self._app.session
        item = server.resolve(session.library, value, verb="pairing")
        bundle = session.pairings.resolve(item, session.library.roots)
        return Response.success(server.describe_pairing(item, bundle))

    def set_still(self, value: str | None) -> Response:
        """`still <wallpaper> <picture>`, or `default` to stop choosing.

        The wallpaper is resolved against the library, so a record can only
        name something the scan produced. The picture is not: a representative
        is a picture, not a library entry, and requiring it to be indexed would
        mean a photo has to be imported before it can stand in for anything.
        """
        source, chosen = server.parse_pair(value, verb="still")
        session = self._app.session
        item = server.resolve(session.library, source, verb="still")
        still: Path | None = None
        if chosen != "default":
            still = server.parse_path(chosen, verb="still")
            if not still.is_file():
                raise ValueError(f"no such picture: {still}")
        session.pairings.choose_still(item, still)
        self._app.pairing_changed(item)
        return Response.success(
            f"{item.name} uses {still.name}" if still else f"{item.name} works its own still out"
        )

    def set_palette(self, value: str | None) -> Response:
        """`palette <wallpaper> <policy>` -- adaptive, keep, or `source:name`."""
        source, encoded = server.parse_pair(value, verb="palette")
        session = self._app.session
        item = server.resolve(session.library, source, verb="palette")
        policy = pairings.PalettePolicy.decode(encoded)
        if policy.encode() != encoded:
            raise ValueError(f"not a palette policy: {encoded}")
        session.pairings.choose_palette(item, policy)
        self._app.pairing_changed(item)
        return Response.success(f"{item.name} asks for {policy.encode()}")

    def reset_pairing(self, value: str | None) -> Response:
        """Forget every choice made for one wallpaper."""
        session = self._app.session
        item = server.resolve(session.library, value, verb="reset-pairing")
        if not session.pairings.reset(item):
            return Response.success(f"{item.name} had nothing customized")
        self._app.pairing_changed(item)
        return Response.success(f"{item.name} is back to its defaults")

    def add_favourite(self, value: str | None) -> Response:
        """Star a wallpaper the library knows about.

        Resolved against the library, so the state file can only ever fill with
        paths the scan produced. A star on something we cannot see would be a
        line in a file with no tile, no rotation entry and nothing to take it
        off again.
        """
        item = server.resolve(self._app.session.library, value, verb="favourite")
        return self._star(item.path, wanted=True)

    def remove_favourite(self, value: str | None) -> Response:
        """Unstar a path, whether or not the library still has it.

        The asymmetry with `favourite` is deliberate and is the whole reason
        this one does not resolve. `library.favourites` keeps an entry whose
        file has gone -- an unmounted drive, a root taken out of the settings
        -- precisely so the list is not silently pruned, and taking the star
        off by hand is the only thing left to do with one. Resolving here would
        make the entries you most want to remove the ones you cannot.
        """
        return self._star(server.parse_path(value, verb="unfavourite"), wanted=False)

    def _star(self, path: Path, *, wanted: bool) -> Response:
        """Move one star, and leave the window agreeing with the store.

        The session's store, never a new one: the window's tiles and the
        rotation are built from that object, and a second copy here would mean
        `ctl favourite` and the star on the tile disagreeing until the next
        launch.
        """
        store = self._app.session.favourites
        try:
            moved = store.add(path) if wanted else store.discard(path)
        except favourites.FavouritesError as error:
            # The store takes the change in memory whatever the disk did, so
            # the window still has to be told; the failure is only about
            # whether the star outlives the session, and it travels with the
            # `local-io` kind that says so.
            self._app.favourites_changed()
            return server.failed(error)
        self._app.favourites_changed()
        if wanted:
            return Response.success(
                f"{path.name} starred" if moved else f"{path.name} was already starred"
            )
        return Response.success(
            f"{path.name} unstarred" if moved else f"{path.name} was not starred"
        )

    def remove_wallpaper(self, value: str | None) -> Response:
        """Delete a downloaded wallpaper, or trash one of the user's own.

        There is no confirmation dialogue on a socket, so the refusals in
        `library.manage` are the whole of the protection and nothing here may
        weaken them: the path is resolved against the library first, so a
        string naming no wallpaper never reaches `manage` at all, ownership is
        re-derived from disk there, and there is no flag that turns any of it
        off. A failure arrives carrying the `kind` that says which refusal it
        was -- `not-ours` for the user's own file, `missing` for one already
        gone.
        """
        session = self._app.session
        item = server.resolve(session.library, value, verb="remove")
        message = server.remove_wallpaper(item, session.library.roots)
        self._app.forget(item.path)
        return Response.success(message)

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
