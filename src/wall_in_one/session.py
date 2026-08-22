"""The running state of the manager: what is in the library and what is on screen.

Deliberately free of GTK. Everything here is callable from a test with no
display and no compositor; the UI layer adds a window and a timer on top and
nothing else. That split is what lets the control verbs -- which is the entire
surface the Noctalia plugin drives -- be tested directly.
"""

from __future__ import annotations

import contextlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wall_in_one import config
from wall_in_one.library import (
    displays,
    favourites,
    pairings,
    playlists,
    scan,
    schedules,
    stills,
    workshop,
)
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.playlist import Playlist
from wall_in_one.library.playlists import Playlist as NamedPlaylist
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer, scenes
from wall_in_one.wallpaper.applier import Applied, Applier, ApplyError

Scanner = Callable[[Sequence[Path] | None], Library]


@dataclass(frozen=True, slots=True)
class LibraryScan:
    """One immutable library-scan request, safe to execute off the UI thread.

    Pairing records and Workshop roots are captured before the worker starts.
    That matters when a setting or pairing changes while a slow filesystem is
    still being walked: the application can tag this request as stale and run
    a newer snapshot, rather than letting one scan mix old and new state.

    An injected scanner is retained for headless callers and tests.  The real
    scanner receives every input explicitly, including Steam roots, so a
    worker never reaches out to mutable ``Session`` state or re-resolves HOME.
    """

    roots: tuple[Path, ...] | None
    records: dict[str, pairings.Pairing]
    include_workshop: bool
    workshop_roots: tuple[Path, ...]
    scanner: Scanner | None = None

    def run(self) -> Library:
        if self.scanner is not None:
            return self.scanner(self.roots)
        return scan.scan(
            self.roots,
            self.records,
            include_workshop=self.include_workshop,
            workshop_roots=self.workshop_roots,
        )


#: The one-entry playlist created when Media is activated.  A fixed id means
#: repeated choices replace one visible playlist instead of filling the store
#: with disposable records.
QUICK_CHOICE_ID = "quick-choice"
QUICK_CHOICE_NAME = "Quick choice"


def _renderer_for(settings: config.Settings) -> renderer.Renderer:
    """A video renderer carrying the user's playback settings from the start.

    Set at construction rather than pushed afterwards, because `when_hidden`
    becomes a command-line flag of mpvpaper's and cannot be changed once a
    video is playing.
    """
    return renderer.Renderer(
        output=settings.output or renderer.ALL_OUTPUTS,
        when_hidden=settings.video_when_hidden,
        hardware_decode=settings.video_hardware_decode,
        interpolation=settings.video_interpolation,
        muted=settings.video_muted,
        volume=settings.video_volume,
    )


class Session:
    """Library, play order, and the wallpaper currently applied."""

    def __init__(
        self,
        settings: config.Settings,
        *,
        applier: Applier | None = None,
        scanner: Scanner | None = None,
        rng: random.Random | None = None,
        favourite_store: favourites.Store | None = None,
        pairing_store: pairings.Store | None = None,
        playlist_store: playlists.Store | None = None,
        schedule_store: schedules.Store | None = None,
        display_store: displays.Store | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        # Only when we build the renderer ourselves: an applier handed in has
        # been configured by whoever handed it in, and reaching into it would
        # overwrite that.
        self._applier = (
            applier
            if applier is not None
            else Applier(
                _renderer_for(settings),
                settings.output,
                scenes.SceneRenderer(output=settings.output, fps=settings.scene_fps),
                own_scene_renderer=settings.own_scene_renderer,
            )
        )
        # The default scanner carries the customizations in with it, so pairing
        # happens once, inside the scan. An injected scanner is left alone: a
        # test that hands over a ready-made library means it, and re-resolving
        # would recompute every pairing from a disk the test never wrote to.
        self._scanner = scanner
        self._library = Library(roots=(), items=())
        #: What the schedule last asked for, so a tick can tell whether the
        #: calendar has moved without rebuilding to find out.
        self._in_force = ""
        #: An on-demand playlist selection temporarily sits above the
        #: calendar.  It is intentionally runtime-only: after a service
        #: restart the saved schedule is authoritative again.
        self._manual_playlist: str | None = None
        self._playlist = Playlist(shuffle=settings.shuffle, rng=rng)
        # Owned here rather than by the window, because the rotation is built
        # from them and the window is not allowed to be the only thing that
        # knows. `open` never raises: an unreadable list degrades to none.
        self._favourites = (
            favourite_store if favourite_store is not None else favourites.Store.open()
        )
        # Owned here for the same reason the favourites are: the rotation and
        # the applier are built from what it resolves, so the window cannot be
        # the only thing that knows.
        self._pairings = pairing_store if pairing_store is not None else pairings.Store.open()
        self._playlists = playlist_store if playlist_store is not None else playlists.Store.open()
        self._schedules = schedule_store if schedule_store is not None else schedules.Store.open()
        self._displays = display_store if display_store is not None else displays.Store.open()
        # Injected so a schedule can be tested at three in the morning in
        # December without waiting until then.
        self._now: Callable[[], datetime] = clock if clock is not None else datetime.now

    # -- state -----------------------------------------------------------

    @property
    def settings(self) -> config.Settings:
        return self._settings

    @property
    def library(self) -> Library:
        return self._library

    @property
    def playlist(self) -> Playlist:
        return self._playlist

    @property
    def current(self) -> Applied | None:
        """What *this app* last applied, or ``None`` if it has not applied yet."""
        return self._applier.current

    @property
    def cursor(self) -> MediaItem | None:
        """The wallpaper the Python compatibility driver is pointing at.

        Not the same as :attr:`current`. The Media grid uses this only when no
        valid Rust status snapshot exists; normally the runtime's per-display
        entry ids are the playback truth. In compatibility mode,
        `sync_with_noctalia` and the local navigation actions move it.
        """
        return self._playlist.current()

    # -- library ---------------------------------------------------------

    def refresh(self, roots: Sequence[Path] | None = None) -> Library:
        """Rescan and rebuild the play order, keeping our place if we can.

        With no roots given, the configured ones are used, and only when there
        are none does `library.scan` fall back to asking Noctalia. Resolving it
        here rather than at each call site is what makes every path into a
        rescan -- startup, the refresh button, a finished download -- honour
        the setting without having to remember to.
        """
        return self.adopt_library(self.prepare_scan(roots).run())

    def prepare_scan(self, roots: Sequence[Path] | None = None) -> LibraryScan:
        """Snapshot all inputs for a scan without walking the library.

        GUI code calls this on GTK's thread and runs :meth:`LibraryScan.run`
        elsewhere.  Keeping the snapshot here means the synchronous and async
        paths cannot quietly disagree about configured roots or pairings.
        """
        if roots is None and self._settings.roots:
            roots = self._settings.roots
        resolved_roots = tuple(roots) if roots is not None else None
        include_workshop = self._settings.scan_workshop
        return LibraryScan(
            roots=resolved_roots,
            records=dict(self._pairings.records),
            include_workshop=include_workshop,
            workshop_roots=workshop.steam_roots() if include_workshop else (),
            scanner=self._scanner,
        )

    def adopt_library(self, library: Library) -> Library:
        """Install one completed scan and reconcile the active play order.

        This is deliberately separate from the filesystem work so GUI callers
        can guarantee mutation happens only on GTK's main thread.
        """
        self._library = library
        self._rebuild_playlist()
        return self._library

    @property
    def pairings(self) -> pairings.Store:
        """The customizations. The grid reads them; the applier obeys them."""
        return self._pairings

    @property
    def displays(self) -> displays.Store:
        """Which playlist each screen shows. Empty means "follow the default"."""
        return self._displays

    @property
    def schedules(self) -> schedules.Store:
        """The calendar rules that override the pinned playlist."""
        return self._schedules

    def active_playlist(self) -> str:
        """The one playlist in force: manual choice, schedule, then default."""
        if self._manual_playlist is not None:
            return self._manual_playlist
        return schedules.effective(
            self._schedules.rules, self._settings.active_playlist, self._now()
        )

    @property
    def manual_playlist(self) -> str | None:
        """The temporary on-demand override, or ``None`` while following time."""
        return self._manual_playlist

    @property
    def playlists(self) -> playlists.Store:
        """The named lists. The rotation follows whichever one is in force."""
        return self._playlists

    @property
    def favourites(self) -> favourites.Store:
        """The starred wallpapers. The grid reads it; the rotation obeys it."""
        return self._favourites

    def authoring_faults(self) -> tuple[tuple[str, str], ...]:
        """Unreadable authoring stores, named for a useful headless error.

        Store ``open`` methods deliberately recover to an empty in-memory
        value so the interactive application can still open and help somebody
        repair a damaged file. An unattended runtime-config compilation must
        make a different choice: compiling those empty values would silently
        replace the last known-good automation document.
        """
        stores = (
            ("pairings", self._pairings),
            ("playlists", self._playlists),
            ("schedules", self._schedules),
            ("display assignments", self._displays),
            ("favourites", self._favourites),
        )
        return tuple((name, fault) for name, store in stores if (fault := store.fault))

    def _rebuild_playlist(self) -> None:
        self._in_force = self.active_playlist()
        self._playlist.set_items(self._rotation())

    def _rotation(self) -> tuple[MediaItem, ...]:
        """What `next` walks through.

        Narrowing to favourites is skipped when it would empty the rotation --
        favourites all deleted, or on a drive that is not mounted. A wallpaper
        manager that stops changing the wallpaper is a worse answer to "you
        have no favourites right now" than one that falls back to the library
        and keeps working; the setting is a preference about which wallpapers,
        not an instruction to show none.
        """
        playable = self._library.playable(dynamics_enabled=self._settings.dynamics_enabled)

        # A named playlist is the stronger statement, so it goes first:
        # somebody who built a list and then left "favourites only" on from
        # last week meant the list.
        listed = playlists.rotation(self._playlists, self.active_playlist(), playable)
        if listed is not None:
            return listed

        if not self._settings.cycle_favourites_only:
            return playable
        starred = self._favourites.paths
        chosen = tuple(item for item in playable if item.path in starred)
        return chosen if chosen else playable

    def schedule_changed(self) -> bool:
        """Rebuild if the calendar now asks for a different playlist.

        Answers whether anything moved, so the caller polling this on a timer
        can avoid redrawing a window every thirty seconds for nothing.
        """
        wanted = self.active_playlist()
        if wanted == self._in_force:
            return False
        self._in_force = wanted
        self._rebuild_playlist()
        return True

    def playlists_changed(self) -> None:
        """Re-narrow the rotation after a list was edited.

        Unconditional, like `favourites_changed`: only the active list changes
        the rotation, but working out whether the edited one *was* the active
        one is more code than rebuilding, and getting it wrong means a list
        that quietly does not take effect.
        """
        self._rebuild_playlist()

    def use_playlist(self, reference: str) -> NamedPlaylist:
        """Play one named list now, temporarily overriding schedule rules."""
        chosen = self._playlists.find(reference)
        self._manual_playlist = chosen.id
        self._rebuild_playlist()
        return chosen

    def resume_schedule(self) -> None:
        """Release an on-demand choice and return control to the calendar."""
        self._manual_playlist = None
        self._rebuild_playlist()

    def favourites_changed(self) -> None:
        """Re-narrow the rotation after a star moved.

        Only matters while `cycle_favourites_only` is on, but calling it
        unconditionally is what stops the rotation and the stars drifting
        apart the moment somebody turns the setting on later.
        """
        self._rebuild_playlist()

    def sync_with_noctalia(self) -> bool:
        """Move the cursor onto whatever wallpaper is actually up.

        Noctalia, or the user, can change the wallpaper behind our back. Without
        this, the next `next` jumps somewhere unrelated instead of continuing
        from what is on screen.
        """
        try:
            active = noctalia.current_wallpaper()
        except noctalia.NoctaliaError:
            return False
        if active is None:
            return False
        if self._playlist.select(active):
            return True
        # It may be a video's paired still rather than a library item itself.
        for item in self._playlist.items:
            if item.paired_still == active:
                return self._playlist.select(item.path)
        return False

    # -- navigation ------------------------------------------------------

    def _apply(self, item: MediaItem | None) -> Applied:
        if item is None:
            raise ApplyError("the library is empty")
        # The pairing is resolved here rather than in the applier, because the
        # store is the session's and the applier has no business reading files.
        bundle = self._pairings.resolve(item, self._library.roots)
        return self._applier.apply(
            item,
            dynamics_enabled=self._settings.dynamics_enabled,
            palette=bundle.palette,
            generator=self._settings.preview_scheme,
        )

    def apply_current(self) -> Applied:
        return self._apply(self._playlist.current())

    def next(self) -> Applied:
        return self._apply(self._playlist.next())

    def previous(self) -> Applied:
        return self._apply(self._playlist.previous())

    def random(self) -> Applied:
        return self._apply(self._playlist.random())

    def choose(self, path: Path) -> NamedPlaylist:
        """Make ``path`` the visible one-entry playlist without applying it.

        The Python app is an authoring client now.  Keeping this step separate
        lets it compile the Quick choice into the Rust runtime config and ask
        that runtime to play it, instead of racing the service with its own
        applier.  ``select`` below remains the legacy-service fallback.
        """
        item = self._library.find(path)
        if item is None:
            raise ApplyError(f"not in the library: {path}")
        try:
            chosen = self._playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, item.path)
        except playlists.PlaylistError as error:
            raise ApplyError(str(error)) from error
        self.use_playlist(QUICK_CHOICE_ID)
        return chosen

    def select(self, path: Path) -> Applied:
        self.choose(path)
        return self.apply_current()

    # -- settings --------------------------------------------------------

    def update_settings(self, settings: config.Settings, *, rescan_library: bool = True) -> None:
        """Adopt new settings, reacting to the ones that change behaviour."""
        previous = self._settings
        self._settings = settings

        if settings.shuffle != previous.shuffle:
            self._playlist.set_shuffle(settings.shuffle)

        if settings.own_scene_renderer != previous.own_scene_renderer:
            self._applier.own_scene_renderer = settings.own_scene_renderer

        if settings.output != previous.output:
            # Both halves, because a still and a video reach the screen by
            # different routes and only one of them can be retuned live.
            self._applier.output = settings.output
            self._applier.renderer.output = settings.output or renderer.ALL_OUTPUTS
            self._applier.scenes.output = settings.output

        if (settings.cycle_favourites_only, settings.active_playlist) != (
            previous.cycle_favourites_only,
            previous.active_playlist,
        ):
            self._rebuild_playlist()

        if (settings.roots, settings.scan_workshop) != (
            previous.roots,
            previous.scan_workshop,
        ) and rescan_library:
            # Nothing else notices: the library is only re-read when something
            # asks, and a root or Workshop source the user just changed would
            # stay stale until the next launch. GUI callers opt out here and
            # submit the exact same scan through their background lane.
            self.refresh()

        if (settings.video_muted, settings.video_volume) != (
            previous.video_muted,
            previous.video_volume,
        ):
            # Over mpv's IPC, so the video keeps playing. Restarting mpvpaper
            # to change the volume would blink the wallpaper, which is a
            # ludicrous price for a slider.
            self._applier.renderer.apply_audio(
                muted=settings.video_muted, volume=settings.video_volume
            )

        if settings.video_when_hidden != previous.video_when_hidden:
            # This one is a launch flag of mpvpaper's, not an mpv property, so
            # it cannot be retuned live. Recording it is enough: the next video
            # starts under the new policy, and saying so beats restarting the
            # wallpaper underneath someone.
            self._applier.renderer.when_hidden = settings.video_when_hidden

        if (
            settings.video_hardware_decode,
            settings.video_interpolation,
        ) != (
            previous.video_hardware_decode,
            previous.video_interpolation,
        ):
            # Both are launch-time mpv options. The Rust runtime reloads and
            # reapplies the current entry; the Python compatibility renderer
            # records them for its next video without blinking this one.
            self._applier.renderer.hardware_decode = settings.video_hardware_decode
            self._applier.renderer.interpolation = settings.video_interpolation

        if settings.scene_fps != previous.scene_fps:
            # This is a launch-time linux-wallpaperengine setting. The Rust
            # runtime reloads the compiled config and performs the active
            # hand-over; this legacy fallback adopts it for the next scene.
            self._applier.scenes.fps = settings.scene_fps

        if settings.dynamics_enabled != previous.dynamics_enabled:
            # Pausing a video with no still used to mean jumping to an unrelated
            # wallpaper. Take a still from it first: a third of a second of
            # ffmpeg, once per video, against the thing the user is watching
            # being swapped out from under them.
            rescued = (
                None if settings.dynamics_enabled else self._still_for_the_video_being_paused()
            )
            # The playable set changes with dynamics: videos with no still drop
            # out when they are off, and come back when they are on.
            self._rebuild_playlist()
            if self._applier.set_dynamics(settings.dynamics_enabled) is None:
                # The wallpaper that was up cannot be shown any more -- an
                # unpaired video being paused. Show its new still if we just
                # made one, and otherwise fall back to whatever the playlist
                # landed on rather than leaving a dead screen.
                with contextlib.suppress(ApplyError):
                    if rescued is not None:
                        self.select(rescued)
                    else:
                        self.apply_current()

    def _still_for_the_video_being_paused(self) -> Path | None:
        """Take a still from the playing video, and return the video's path.

        ``None`` when there is nothing to do or nothing can be done -- already
        paired, not a video, no root to write into, or ffmpeg refusing the
        file. None of those is worth reporting: the fallback below still puts
        *a* wallpaper on screen, which is what pausing has always done.

        The rescan is what makes the new still count. `paired_still` is fixed
        on the `MediaItem` at scan time, so the library has to be re-read
        before anything downstream can see the pairing that was just written.
        """
        current = self._applier.current
        if current is None or current.item.kind is not Kind.VIDEO:
            return None
        if current.item.paired_still is not None:
            return None
        roots = self._library.roots
        if not roots:
            return None
        if stills.ensure(current.item, roots[0]) is None:
            return None
        self.refresh()
        return current.item.path

    def shutdown(self) -> None:
        self._applier.shutdown()

    # -- reporting -------------------------------------------------------

    def describe(self) -> str:
        current = self.current
        showing = current.describe() if current is not None else "nothing applied"
        return (
            f"{showing}; {len(self._playlist)} of {len(self._library)} playable; "
            f"shuffle={'on' if self._settings.shuffle else 'off'} "
            f"cycle={'on' if self._settings.cycle_enabled else 'off'} "
            f"cycle-interval={self._settings.cycle_interval} "
            f"dynamics={'on' if self._settings.dynamics_enabled else 'off'}"
        )
