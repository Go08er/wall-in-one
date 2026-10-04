"""What the new interface's Playlists page needs from the app.

Creating, renaming, reordering and removing playlists and their entries, a
playlist's own interval and shuffle, the schedule default, and Play now.
`AppState.playlist_editing` returns a `PlaylistEditing`, or None while the
app can't offer it; the page then shows its "not in the new interface yet"
placeholder. The real one lives beside the adapter in ``real_playlists.py``;
the prototype's demo state is the other.

**What the page shows.** `PlaylistEditing.detail` is the playlist as it is
saved, plus what is still being saved: a rename, an interval or shuffle
choice and the default show the latest value asked for until every write
for it has settled, then the saved one (`wall_in_one.ui.pending_edits`).
Entries are always the saved ones; ``busy`` says that changes to them are
still queued, so a page that moved a row under the pointer keeps it there
until they settle. Every write settles with a ``changed`` signal:
``playlists`` when the store changed, ``playlist-edits`` when only what is
being saved did (a refusal, a failure, a move that changed nothing).

**Automatic playlists.** "All wallpapers" (the library) and the hidden
"Your pick" playlists are never edited here: `detail` is None for them.

GTK-free: annotations, records and words only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

#: The intervals the page offers for a playlist's own rotation, in seconds.
#: A playlist with a different saved value (set from ``ctl``) shows that too.
INTERVALS: Final = (
    5 * 60,
    15 * 60,
    30 * 60,
    60 * 60,
    2 * 60 * 60,
    6 * 60 * 60,
    24 * 60 * 60,
)

#: The topic a write emits when only what is being saved changed.
EDITS_TOPIC: Final = "playlist-edits"

#: Where a playlist is used: a schedule rule, the schedule's default, a display.
UsageKind = Literal["rule", "default", "display"]


def interval_words(seconds: int) -> str:
    """``Every 15 minutes``, ``Every hour``, ``Once a day``, ``Every 90 seconds``."""
    if seconds == 24 * 60 * 60:
        return "Once a day"
    if seconds % (60 * 60) == 0:
        hours = seconds // (60 * 60)
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    return f"Every {seconds} seconds"


@dataclass(frozen=True, slots=True)
class EntryView:
    """One position in a playlist. One wallpaper may fill several."""

    #: The entry's own id: what survives reordering, and what Undo restores.
    id: str
    #: The wallpaper's id (`AppState.wallpaper`), also when it is missing.
    wallpaper: str
    #: Its name, or the file's name when the library doesn't have it now.
    name: str
    #: Not in the library now (a drive that isn't mounted); the entry stays.
    missing: bool = False
    #: Why playback skips it, or empty.
    problem: str = ""

    @property
    def usable(self) -> bool:
        """Whether playback can show it."""
        return not self.missing and not self.problem


@dataclass(frozen=True, slots=True)
class Usage:
    """One place a playlist is used, for the page's "Used by" chips."""

    kind: UsageKind
    label: str
    tooltip: str = ""
    #: A schedule rule that is turned off.
    off: bool = False
    #: The page the chip opens: ``schedule`` or ``displays``.
    target: str = "schedule"


@dataclass(frozen=True, slots=True)
class Rotation:
    """How a playlist changes wallpaper: its own choice, or the default."""

    #: Its own interval in seconds, or None to use the default.
    interval: int | None = None
    #: Its own shuffle, or None to use the default.
    shuffle: bool | None = None
    #: The defaults (Settings), for the "Default (…)" choice.
    default_interval: int = 300
    default_shuffle: bool = False
    #: Something the rows need to say (when it applies), or empty.
    note: str = ""


@dataclass(frozen=True, slots=True)
class PlaylistDetail:
    """One playlist the page edits."""

    id: str
    name: str
    entries: tuple[EntryView, ...]
    usage: tuple[Usage, ...] = ()
    #: ``On screen now · your pick``, ``On DP-1 now · from schedule``, or empty.
    on_screen: str = ""
    #: Shown now as a pick on every display it reaches: Play now has nothing to do.
    playing: bool = False
    #: ``(entry id, connector)`` for each display showing one of its entries now.
    showing: tuple[tuple[str, str], ...] = ()
    #: Why Play now can't run, or empty.
    play_blocked: str = ""
    #: Plays whenever no schedule rule applies (the default).
    is_default: bool = False
    #: Why it can't become the default, or empty.
    default_blocked: str = ""
    rotation: Rotation = field(default_factory=Rotation)
    #: Changes to its entries are still being saved.
    busy: bool = False

    @property
    def usable(self) -> int:
        """Entries playback can show (a wallpaper listed twice counts twice)."""
        return sum(entry.usable for entry in self.entries)

    @property
    def unavailable(self) -> int:
        """Entries that are missing or skipped."""
        return len(self.entries) - self.usable


class PlaylistEditing(Protocol):
    """The Playlists page's view of the app.

    Each action reports its own refusals and failures; nothing a page shows
    changes until the store or `detail` says so. Actions name a playlist by
    id and never touch automatic ones.
    """

    def detail(self, pid: str) -> PlaylistDetail | None:
        """The playlist to edit, or None (automatic, hidden or gone)."""
        ...

    def editing_blocked(self) -> str:
        """Why no playlist can change now (files from a newer version), or empty."""
        ...

    def creating(self, pid: str) -> bool:
        """A playlist asked for by `create_playlist` or `duplicate_playlist`, not saved yet."""
        ...

    def create_playlist(self, name: str) -> str:
        """Ask for a new, empty playlist. Its id comes back now; it exists once saved."""
        ...

    def duplicate_playlist(self, pid: str) -> str:
        """Ask for a copy (its own entries, interval and shuffle). Its id comes back now."""
        ...

    def rename_playlist(self, pid: str, name: str) -> None: ...

    def delete_warning(self, pid: str) -> str:
        """What deleting it also removes, for the confirmation."""
        ...

    def delete_playlist(self, pid: str) -> None: ...

    def add_entries(self, pid: str, wids: Sequence[str], position: int | None = None) -> None:
        """Add wallpapers in this order at ``position`` (None: the end), in one save."""
        ...

    def remove_entries(self, pid: str, entry_ids: Sequence[str]) -> None:
        """Take entries out, in one save; once it is saved a toast offers Undo."""
        ...

    def move_entry(self, pid: str, entry_id: str, step: int) -> None:
        """Move an entry ``step`` places (up when negative) from where it is when saved."""
        ...

    def set_playlist_interval(self, pid: str, seconds: int | None) -> None:
        """Its own interval, or None for the default."""
        ...

    def set_playlist_shuffle(self, pid: str, shuffle: bool | None) -> None:
        """Its own shuffle, or None for the default."""
        ...

    def make_default(self, pid: str) -> None:
        """Play it whenever no schedule rule applies."""
        ...

    def play(self, pid: str) -> None:
        """Show it on every display until the schedule is resumed (a pick)."""
        ...


def usage_words(usage: Sequence[Usage]) -> str:
    """``the schedule default, 2 schedule rules and HDMI-A-1``, for refusals."""
    rules = sum(item.kind == "rule" for item in usage)
    parts = ["the schedule default"] if any(item.kind == "default" for item in usage) else []
    if rules:
        parts.append("a schedule rule" if rules == 1 else f"{rules} schedule rules")
    parts.extend(item.label for item in usage if item.kind == "display")
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
