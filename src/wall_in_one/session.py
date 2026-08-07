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
from pathlib import Path

from wall_in_one import config
from wall_in_one.library import favourites, pairings, scan, stills
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.playlist import Playlist
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer
from wall_in_one.wallpaper.applier import Applied, Applier, ApplyError

Scanner = Callable[[Sequence[Path] | None], Library]


def _renderer_for(settings: config.Settings) -> renderer.Renderer:
    """A video renderer carrying the user's playback settings from the start.

    Set at construction rather than pushed afterwards, because `when_hidden`
    becomes a command-line flag of mpvpaper's and cannot be changed once a
    video is playing.
    """
    return renderer.Renderer(
        output=settings.output or renderer.ALL_OUTPUTS,
        when_hidden=settings.video_when_hidden,
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
    ) -> None:
        self._settings = settings
        # Only when we build the renderer ourselves: an applier handed in has
        # been configured by whoever handed it in, and reaching into it would
        # overwrite that.
        self._applier = (
            applier if applier is not None else Applier(_renderer_for(settings), settings.output)
        )
        # The default scanner carries the customizations in with it, so pairing
        # happens once, inside the scan. An injected scanner is left alone: a
        # test that hands over a ready-made library means it, and re-resolving
        # would recompute every pairing from a disk the test never wrote to.
        self._scan: Scanner = scanner if scanner is not None else self._scan_with_pairings
        self._library = Library(roots=(), items=())
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
        """The wallpaper the app is pointing at -- what the grid highlights.

        Not the same as :attr:`current`. At startup nothing has been applied
        through us, but `sync_with_noctalia` has already moved the cursor onto
        whatever is actually on screen, and that is the wallpaper the user
        expects to see marked. `next` and friends move this too.
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
        if roots is None and self._settings.roots:
            roots = self._settings.roots
        self._library = self._scan(roots)
        self._rebuild_playlist()
        return self._library

    def _scan_with_pairings(self, roots: Sequence[Path] | None) -> Library:
        return scan.scan(roots, self._pairings.records)

    @property
    def pairings(self) -> pairings.Store:
        """The customizations. The grid reads them; the applier obeys them."""
        return self._pairings

    @property
    def favourites(self) -> favourites.Store:
        """The starred wallpapers. The grid reads it; the rotation obeys it."""
        return self._favourites

    def _rebuild_playlist(self) -> None:
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
        if not self._settings.cycle_favourites_only:
            return playable
        starred = self._favourites.paths
        chosen = tuple(item for item in playable if item.path in starred)
        return chosen if chosen else playable

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

    def select(self, path: Path) -> Applied:
        if not self._playlist.select(path):
            raise ApplyError(f"not in the library: {path}")
        return self.apply_current()

    # -- settings --------------------------------------------------------

    def update_settings(self, settings: config.Settings) -> None:
        """Adopt new settings, reacting to the ones that change behaviour."""
        previous = self._settings
        self._settings = settings

        if settings.shuffle != previous.shuffle:
            self._playlist.set_shuffle(settings.shuffle)

        if settings.output != previous.output:
            # Both halves, because a still and a video reach the screen by
            # different routes and only one of them can be retuned live.
            self._applier.output = settings.output
            self._applier.renderer.output = settings.output or renderer.ALL_OUTPUTS

        if settings.cycle_favourites_only != previous.cycle_favourites_only:
            self._rebuild_playlist()

        if settings.roots != previous.roots:
            # Nothing else notices: the library is only re-read when something
            # asks, and a root the user just added would stay invisible until
            # the next launch.
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
