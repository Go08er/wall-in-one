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

**Hiding paths** (`PathHider`) never guesses where a path ends in free text.
It replaces the folders it knows, wherever they appear, with a stable name:
the home folder becomes ``~``, each library folder ``<library 1>``,
``<library 2>`` ... (numbered as the session first saw it, and kept after it
is removed), and the XDG config, state, cache and data folders and the runtime
folder ``<config>``, ``<state>``, ``<cache>``, ``<data>`` and ``<runtime>``.
The longest wins, so a library inside the home folder reads ``<library 1>``.
Each is matched as written, with spaces, quotes or any other characters in
it, and in the other forms messages quote paths in: through symlinks (the
home folder's real path), in a ``file://`` URI, and escaped inside a quoted
string (JSON, Python, Rust). Last, the user's login name is replaced wherever
it stands as a word, so it can't survive in some form not listed. What
follows a known folder (a file's name) stays readable. Entries keep their
real text, so hiding can be turned off again; it applies to what is shown
and copied.

GTK-free and main-thread only, like the status model it reads.
"""

from __future__ import annotations

import datetime
import getpass
import json
import os
import re
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from wall_in_one import paths
from wall_in_one.ui.status_model import RuntimeStatusView, StatusChange

#: Entries kept; the oldest go first.
MAX_ENTRIES: Final = 500
#: Characters kept of one entry's text (a renderer's error can be long).
MAX_TEXT: Final = 2000
#: What the user's login name reads as, wherever it is a word of its own.
USER: Final = "<user>"

Level = Literal["info", "warning", "error"]

#: A known folder is matched wherever it starts (behind a mount prefix too),
#: but not where its last name goes on: ``/home/ann`` is not ``/home/anna``
#: or ``/home/ann.old`` (a sentence's full stop may follow it).
_AFTER: Final = r"(?![\w\-]|\.[\w\-])"


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


def spellings(path: str, *, resolve: bool = False) -> set[str]:
    """How a message may spell ``path``: as written (and, with ``resolve``,
    through its symlinks), in a ``file://`` URI, or escaped inside a quoted
    string as JSON, Python's ``repr`` or Rust's ``{:?}`` write it."""
    written = {path}
    if resolve:
        written.add(os.path.realpath(path))
    found: set[str] = set()
    for spelling in written:
        found.add(spelling)
        found.add(urllib.parse.quote(spelling, safe="/"))
        found.add(json.dumps(spelling)[1:-1])
        found.add(json.dumps(spelling, ensure_ascii=False)[1:-1])
        found.add(repr(spelling)[1:-1])
        backslashed = spelling.replace("\\", "\\\\")
        found.add(backslashed.replace("'", "\\'"))
        found.add(backslashed.replace('"', '\\"'))
    return {spelling for spelling in found if len(spelling) > 1}


def _folder(path: Path | str) -> str:
    """``path`` as a folder to look for: absolute, no trailing slash, never ``/``."""
    text = str(path).rstrip("/")
    return text if text.startswith("/") else ""


def _login_name() -> str:
    try:
        return getpass.getuser()
    except KeyError, OSError:
        return Path.home().name


class PathHider:
    """Replaces known folders, and the login name, in text (see the module notes)."""

    def __init__(
        self,
        libraries: Sequence[str] = (),
        *,
        home: str | None = None,
        places: Iterable[tuple[str, str]] | None = None,
        user: str | None = None,
    ) -> None:
        home = _folder(home if home is not None else Path.home())
        if places is None:
            places = (
                (str(paths.config_home()), "<config>"),
                (str(paths.state_home()), "<state>"),
                (str(paths.cache_home()), "<cache>"),
                (str(paths.data_home()), "<data>"),
                (os.environ.get("XDG_RUNTIME_DIR", ""), "<runtime>"),
            )
        names: dict[str, str] = {}
        # Libraries first: on equal spellings the more specific name wins.
        for number, library in enumerate(libraries, start=1):
            for spelling in spellings(_folder(library)):
                names.setdefault(spelling, f"<library {number}>")
        for place, name in places:
            for spelling in spellings(_folder(place)):
                names.setdefault(spelling, name)
        if home:
            # The home folder's real path too: on some systems /home is a link.
            for spelling in spellings(home, resolve=True):
                names.setdefault(spelling, "~")
        self._names = names
        # Longest first: an alternation takes the first branch that matches.
        ordered = sorted(names, key=len, reverse=True)
        self._folders = (
            re.compile("(?:" + "|".join(map(re.escape, ordered)) + ")" + _AFTER)
            if ordered
            else None
        )
        login = user if user is not None else _login_name()
        logins = sorted(spellings(login), key=len, reverse=True) if len(login) > 1 else []
        # A name already put in place is skipped (a login could be "library").
        self._login = (
            re.compile(
                r"<[^<>\s]+(?: \d+)?>|(?<!\w)(?:" + "|".join(map(re.escape, logins)) + r")(?!\w)"
            )
            if logins
            else None
        )

    def __call__(self, text: str) -> str:
        if self._folders is not None:
            text = self._folders.sub(lambda match: self._names[match.group()], text)
        if self._login is not None:
            text = self._login.sub(
                lambda match: match.group() if match.group().startswith("<") else USER, text
            )
        return text

    def safe_cut(self, text: str, cut: int) -> int:
        """``cut``, moved back so that cutting ``text`` there splits no known folder or name."""
        for pattern in (self._folders, self._login):
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                if match.start() < cut < match.end():
                    cut = match.start()
        return cut


def one_line(text: str) -> str:
    """``text`` on one line, as the log shows each entry."""
    return " ".join(text.split())


Listener = Callable[[Entry], None]


class ActivityLog:
    """A bounded, in-memory log, and what changes in the runtime status say to it."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        limit: int = MAX_ENTRIES,
        folders: Callable[[], Iterable[Path]] = tuple,
    ) -> None:
        self._clock = clock
        self._entries: deque[Entry] = deque(maxlen=limit)
        #: The library folders, as `folders` gives them when asked.
        self._folders = folders
        #: Every library folder this session has seen, first seen first.
        self._libraries: list[str] = []
        self._hider: PathHider | None = None
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

    # -- hiding paths --------------------------------------------------------------------
    def _note_folders(self) -> None:
        """Remember the library folders now: a line may name one later removed."""
        for folder in self._folders():
            text = _folder(folder)
            if text and text not in self._libraries:
                self._libraries.append(text)
                self._hider = None

    @property
    def hider(self) -> PathHider:
        """The `PathHider` for every library folder seen so far (kept once seen)."""
        self._note_folders()
        if self._hider is None:
            self._hider = PathHider(self._libraries)
        return self._hider

    def shown(self, entry: Entry, *, hide: bool) -> str:
        """``entry``'s words as the log shows and copies them: one line, maybe hidden."""
        return one_line(self.hider(entry.text) if hide else entry.text)

    def as_text(self, *, hide: bool) -> str:
        """Every entry as a plain line, for the clipboard."""
        return "\n".join(
            f"{entry.time:%H:%M:%S}  {entry.source:<9} {self.shown(entry, hide=hide)}"
            for entry in self._entries
        )

    # -- recording ----------------------------------------------------------------------
    def record(self, source: str, text: str, level: Level = "info") -> None:
        """Add one entry, now; empty text adds nothing.

        The words are kept as written (a path's spaces included), so hiding
        sees them whole; a long entry is cut where no known folder is split.
        """
        text = text.strip()
        if not text:
            return
        self._note_folders()
        if len(text) > MAX_TEXT:
            text = text[: self.hider.safe_cut(text, MAX_TEXT - 1)] + "\u2026"
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
