"""Which runtime verb Play sends, and when it must send none: one rule for every window.

The classic window's playback popover, the classic Schedules page's display
rows and the new player bar decide the same way, from the runtime's own
snapshot at the moment of the press:

* a renderer that stopped is retried with ``play``: the runtime has no
  ``retry`` verb, and Play is its one-shot retry;
* displays that disagree (``mixed``) are brought together with ``toggle``,
  which pauses every display if any is playing and otherwise plays them all;
* otherwise Play pauses what plays and plays what is paused or stopped.

A wallpaper in the runtime's session taboo set (its renderer kept failing,
so playback skips it) is never sent Play: the runtime refuses to retry it.
Each window leads to the wallpaper in the Library instead.

GTK-free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from wall_in_one.ui import runtime_truth

#: The runtime's overall playback states; ``mixed`` only at the top level.
PLAYBACK_STATES: Final = ("playing", "paused", "stopped", "mixed")


def playback_state(status: Mapping[str, object]) -> str:
    """The snapshot's overall state: ``playback_state``, or an older runtime's ``paused``."""
    reported = status.get("playback_state")
    if isinstance(reported, str) and reported in PLAYBACK_STATES:
        return reported
    return "paused" if status.get("paused") is True else "playing"


def play_verb(state: str, *, renderer_failed: bool) -> str:
    """The verb Play sends for ``state`` (one display's, or the snapshot's overall)."""
    if renderer_failed:
        return "play"
    if state == "mixed":
        return "toggle"
    return "pause" if state == "playing" else "play"


def display_is_taboo(
    status: Mapping[str, object] | None, display: runtime_truth.DisplayRuntimeTruth
) -> bool:
    """Whether playback skips what ``display`` shows, so Play must not be sent to it."""
    return display.entry_taboo or runtime_truth.entry_is_taboo(
        status, display.playlist_id, display.entry_id
    )
