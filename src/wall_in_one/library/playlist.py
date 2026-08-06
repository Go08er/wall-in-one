"""Play order: what comes next, what came before, and shuffle.

Pure logic, no timers and no I/O. The cycle timer lives in the UI layer where
there is a main loop to hang it off; this module only answers "which one now".

Shuffle is a permutation, not a dice roll per step. That means `next` visits
every wallpaper once before repeating, and `prev` retraces the exact path you
came down -- both of which a naive random pick gets wrong.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from pathlib import Path

from wall_in_one.library.model import MediaItem


class Playlist:
    """A cursor over a sequence of items, with optional shuffle."""

    def __init__(
        self,
        items: Sequence[MediaItem] = (),
        *,
        shuffle: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        self._items: tuple[MediaItem, ...] = tuple(items)
        self._shuffle = shuffle
        self._rng = rng if rng is not None else random.Random()
        self._order: list[int] = []
        self._cursor = 0
        self._rebuild_order()

    # -- state -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> tuple[MediaItem, ...]:
        return self._items

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def position(self) -> int:
        """Zero-based position in the current play order."""
        return self._cursor

    def current(self) -> MediaItem | None:
        if not self._order:
            return None
        return self._items[self._order[self._cursor]]

    # -- mutation --------------------------------------------------------

    def _rebuild_order(self, *, keep: Path | None = None) -> None:
        self._order = list(range(len(self._items)))
        if self._shuffle:
            self._rng.shuffle(self._order)
        self._cursor = 0
        if keep is not None:
            self.select(keep)

    def set_items(self, items: Sequence[MediaItem]) -> None:
        """Replace the contents, staying on the current item if it survives."""
        current = self.current()
        self._items = tuple(items)
        self._rebuild_order(keep=current.path if current is not None else None)

    def set_shuffle(self, enabled: bool) -> None:
        """Turn shuffle on or off, without losing your place.

        Turning shuffle on reshuffles immediately rather than at the end of the
        pass, so the setting visibly does something.
        """
        if enabled == self._shuffle:
            return
        current = self.current()
        self._shuffle = enabled
        self._rebuild_order(keep=current.path if current is not None else None)

    def select(self, path: Path) -> bool:
        """Move the cursor onto ``path``. False if it is not in the playlist.

        Used to resync after something outside the app changed the wallpaper,
        so the next `next` continues from there instead of jumping.
        """
        for position, index in enumerate(self._order):
            if self._items[index].path == path:
                self._cursor = position
                return True
        return False

    # -- navigation ------------------------------------------------------

    def next(self) -> MediaItem | None:
        """Advance one, reshuffling when a shuffled pass wraps around."""
        if not self._order:
            return None
        self._cursor += 1
        if self._cursor >= len(self._order):
            self._cursor = 0
            if self._shuffle and len(self._order) > 1:
                # A fresh permutation for the next pass, but never repeat the
                # wallpaper that was just on screen across the seam.
                previous_last = self._order[-1]
                self._rng.shuffle(self._order)
                if self._order[0] == previous_last:
                    self._order[0], self._order[-1] = self._order[-1], self._order[0]
        return self.current()

    def previous(self) -> MediaItem | None:
        """Step back one, wrapping to the end."""
        if not self._order:
            return None
        self._cursor = (self._cursor - 1) % len(self._order)
        return self.current()

    def random(self) -> MediaItem | None:
        """Jump somewhere else at random, never to where we already are."""
        if not self._order:
            return None
        if len(self._order) == 1:
            return self.current()
        choices = [position for position in range(len(self._order)) if position != self._cursor]
        self._cursor = self._rng.choice(choices)
        return self.current()
