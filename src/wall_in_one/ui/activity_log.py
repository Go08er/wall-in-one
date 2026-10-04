"""This session's log: what Wall-in-One and its wallpaper service said.

Kept in memory for as long as the application runs, and bounded
(`MAX_ENTRIES`, each entry at most `MAX_TEXT` characters). Nothing is written
to disk and nothing survives a restart, so the log adds no file and no format.
`Application` feeds it from two places:

* every message it shows the user (``Application.window_report``), with its
  words unchanged;
* the runtime's status, as it changes (`ActivityLog.observe`, from the
  application's one forwarding subscriber): the wallpaper service answering or
  going away, a renderer that stopped (in the runtime's own words), degraded
  display discovery, an invalid status reply, a missed status deadline, what
  plays and why, and the power policy. The two-second poll repeats the last
  snapshot; a repeat adds nothing.

It holds what this process saw, not the service's own output: that goes to
the journal of ``wall-in-one.service``, which the app does not read.

`hide_paths` hides file paths for showing and copying: the home directory
becomes ``~`` and every path left becomes `HIDDEN`. Entries keep their real
text, so hiding can be turned off again.

GTK-free and main-thread only, like the status model it reads.
"""

from __future__ import annotations

import datetime
import os
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from wall_in_one.ui.status_model import RuntimeStatusView, StatusChange

#: Entries kept; the oldest go first.
MAX_ENTRIES: Final = 500
#: Characters kept of one entry's text (a renderer's error can be long).
MAX_TEXT: Final = 2000
#: What a hidden path reads as.
HIDDEN: Final = "\u2039path\u203a"

Level = Literal["info", "warning", "error"]

#: An absolute or home-relative path, as far as text tells: up to whitespace,
#: a quote or a colon. Not inside a word or a URL (``https://...``).
_PATH: Final = re.compile(r"(?<![\w:/~.])(?:~(?=/)|/)(?:/?[^\s/:'\"`(),;]+)*/?")


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing said, when, by what."""

    time: datetime.datetime
    #: ``app`` (Wall-in-One's own messages), ``service``, ``renderer``,
    #: ``displays``, ``playback`` or ``power``.
    source: str
    level: Level
    text: str

    @property
    def problem(self) -> bool:
        return self.level != "info"


def hide_paths(text: str) -> str:
    """``text`` with file paths hidden: the home directory first, then any path."""
    home = os.path.expanduser("~").rstrip("/")
    if home:
        text = text.replace(home + "/", "~/")
        text = re.sub(re.escape(home) + r"(?![\w.\-])", "~", text)
    return _PATH.sub(_hidden, text)


def _hidden(match: re.Match[str]) -> str:
    # A sentence's full stop after a path is not part of it.
    return HIDDEN + "." if match.group().endswith(".") else HIDDEN


def as_text(entries: tuple[Entry, ...], *, hide: bool) -> str:
    """The entries as plain lines, for the clipboard."""
    lines = [f"{entry.time:%H:%M:%S}  {entry.source:<9} {entry.text}" for entry in entries]
    joined = "\n".join(lines)
    return hide_paths(joined) if hide else joined


Listener = Callable[[Entry], None]


class ActivityLog:
    """A bounded, in-memory log, and what changes in the runtime status say to it."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        limit: int = MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self._entries: deque[Entry] = deque(maxlen=limit)
        self._listeners: list[tuple[object, Listener]] = []
        # The last status said, so each change is said once.
        self._service = "checking"
        self._playing: tuple[str, str] | None = None
        self._renderer = ""
        self._displays = ""
        self._power = ""
        self._invalid = ""
        self._delayed = False

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    @property
    def problems(self) -> int:
        return sum(1 for entry in self._entries if entry.problem)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` with each new entry; returns its unsubscribe."""
        token = object()
        self._listeners.append((token, listener))

        def unsubscribe() -> None:
            self._listeners[:] = [entry for entry in self._listeners if entry[0] is not token]

        return unsubscribe

    def record(self, source: str, text: str, level: Level = "info") -> None:
        """Add one entry, now; empty text adds nothing."""
        text = " ".join(text.split())
        if not text:
            return
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "\u2026"
        entry = Entry(self._clock(), source, level, text)
        self._entries.append(entry)
        for _token, listener in tuple(self._listeners):
            listener(entry)

    # -- the runtime's status --------------------------------------------------------
    def observe(self, change: StatusChange, view: RuntimeStatusView) -> None:
        """Say what one status publication changed, if anything."""
        match change:
            case StatusChange.STATUS:
                self._status(view)
            case StatusChange.UNAVAILABLE:
                if self._service != "unavailable":
                    self.record("service", "The wallpaper service isn\u2019t running", "warning")
                self._service = "unavailable"
                self._renderer = self._displays = self._invalid = ""
                self._playing = None
                self._delayed = False
            case StatusChange.DELAYED:
                if not self._delayed:
                    self.record(
                        "service",
                        "A status reply was late; the last one is shown until the next",
                        "warning",
                    )
                self._delayed = True
            case StatusChange.INVALID:
                message = view.protocol_error
                if message and message != self._invalid:
                    self.record("service", f"Invalid status reply: {message}", "warning")
                self._invalid = message
            case StatusChange.BUSY:
                pass

    def _status(self, view: RuntimeStatusView) -> None:
        status = view.status
        if status is None:
            return
        if self._service != "running":
            self.record("service", "The wallpaper service is running")
        self._service = "running"
        self._delayed = False
        self._invalid = ""
        playlist, source = status.get("playlist"), status.get("source")
        if isinstance(playlist, str) and isinstance(source, str):
            playing = (playlist, source)
            if playing != self._playing and playlist:
                self.record("playback", f"Playing {playlist} ({source})")
            self._playing = playing
        last_error = status.get("last_error")
        last_error = last_error if isinstance(last_error, str) else ""
        if last_error and last_error != self._renderer:
            self.record("renderer", last_error, "error")
        self._renderer = last_error
        discovery = status.get("output_discovery_error")
        discovery = discovery if isinstance(discovery, str) else ""
        if discovery and discovery != self._displays:
            self.record("displays", f"Display discovery is degraded: {discovery}", "warning")
        self._displays = discovery
        power = view.power
        message = power.message if power is not None else ""
        if message != self._power:
            if message:
                self.record("power", message)
            elif self._power:
                self.record("power", "Animations are no longer stopped for power")
        self._power = message
