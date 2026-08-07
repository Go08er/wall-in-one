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
from wall_in_one.library import scan, stills
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.playlist import Playlist
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper.applier import Applied, Applier, ApplyError

Scanner = Callable[[Sequence[Path] | None], Library]


class Session:
    """Library, play order, and the wallpaper currently applied."""

    def __init__(
        self,
        settings: config.Settings,
        *,
        applier: Applier | None = None,
        scanner: Scanner | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._applier = applier if applier is not None else Applier()
        self._scan: Scanner = scanner if scanner is not None else scan.scan
        self._library = Library(roots=(), items=())
        self._playlist = Playlist(shuffle=settings.shuffle, rng=rng)

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
        """Rescan and rebuild the play order, keeping our place if we can."""
        self._library = self._scan(roots)
        self._rebuild_playlist()
        return self._library

    def _rebuild_playlist(self) -> None:
        self._playlist.set_items(
            self._library.playable(dynamics_enabled=self._settings.dynamics_enabled)
        )

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
        return self._applier.apply(item, dynamics_enabled=self._settings.dynamics_enabled)

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
