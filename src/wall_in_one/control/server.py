"""Control socket server.

Runs inside the GTK main loop via `Gio.SocketService`, so handlers execute on
the main thread and can touch the UI directly without locking.

That is the whole difficulty with `search` and `download`: a handler that waits
for a website on this thread freezes every frame the app draws, for seconds or
for minutes. So a handler may answer with a `Deferred` instead of a `Response`,
which hands the reply to a callback later; the connection simply stays open
until then. `ui.browse_dialog` already puts provider calls on a worker pool and
comes back through `GLib.idle_add`, and the application's implementation of
these verbs does exactly the same -- none of that machinery lives here, because
this module has to stay importable and testable with no display attached.

The verb table is kept separate from the transport (`Commands`) so it can be
tested without a socket or a display. The library verbs added later keep to the
same line: everything that is only a question about paths and files -- which
wallpapers a filter names, what a row looks like, which of the two removals a
file is due -- is here, where a test needs neither. Only what touches the
running session and the window is in `ui.app`.

One rule governs all of it. A path arriving over this socket is text somebody
outside the process typed, and it is turned into a wallpaper by looking it up
in the library rather than by being believed; see `resolve`.
"""

from __future__ import annotations

import contextlib
import os
import socket
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from wall_in_one import paths
from wall_in_one.control.protocol import (
    ENCODING,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Request,
    Response,
)
from wall_in_one.library import filter as library_filter
from wall_in_one.library import manage, pairings, playlists
from wall_in_one.library.model import Library, MediaItem
from wall_in_one.providers import registry
from wall_in_one.providers.base import SearchResult

#: Handed a response, exactly once, whenever it turns up.
Reply = Callable[[Response], None]


@dataclass(frozen=True, slots=True)
class Deferred:
    """An answer that is not ready yet.

    `start` is called with a `Reply` and must arrange for it to be invoked once,
    back on the main thread, when the answer arrives. Returning one of these is
    a handler saying "not on this thread": the caller keeps the client's
    connection open and writes whatever the reply eventually carries.

    A `start` that never calls its reply leaves that client waiting until its
    own timeout, which is the price of not blocking the compositor's idea of a
    responsive window.
    """

    start: Callable[[Reply], None]


#: What a handler answers with: a response now, or the promise of one later.
Outcome = Response | Deferred

Handler = Callable[[str | None], Outcome]


class Commands(Protocol):
    """What the application must provide for the control surface to work.

    Three groups. The first mirrors the Noctalia plugin's control list: launch,
    transport, shuffle, cycle, cycle duration, and the dynamics pause. The
    browsing three are not on the plugin's list -- they exist so that searching
    for a wallpaper and pulling it into the library are reachable from a
    terminal or a script without opening the window. The library six are there
    for the same reason and answer the question the other two groups left out:
    the socket could drive the playback and fill the library, but could not say
    what was in it, star anything, apply one wallpaper by name, or take one
    away.
    """

    def next_wallpaper(self) -> Response: ...
    def previous_wallpaper(self) -> Response: ...
    def random_wallpaper(self) -> Response: ...
    def set_shuffle(self, value: str | None) -> Response: ...
    def set_cycle(self, value: str | None) -> Response: ...
    def set_cycle_interval(self, value: str | None) -> Response: ...
    def set_dynamics(self, value: str | None) -> Response: ...
    def reload_palette(self) -> Response: ...
    def report_status(self) -> Response: ...
    def list_library(self, value: str | None) -> Response: ...
    def select_wallpaper(self, value: str | None) -> Response: ...
    def list_favourites(self) -> Response: ...
    def add_favourite(self, value: str | None) -> Response: ...
    def remove_favourite(self, value: str | None) -> Response: ...
    def remove_wallpaper(self, value: str | None) -> Response: ...
    def show_pairing(self, value: str | None) -> Response: ...
    def set_still(self, value: str | None) -> Response: ...
    def set_palette(self, value: str | None) -> Response: ...
    def reset_pairing(self, value: str | None) -> Response: ...
    def list_playlists(self, value: str | None) -> Response: ...
    def make_playlist(self, value: str | None) -> Response: ...
    def drop_playlist(self, value: str | None) -> Response: ...
    def add_to_playlist(self, value: str | None) -> Response: ...
    def remove_from_playlist(self, value: str | None) -> Response: ...
    def use_playlist(self, value: str | None) -> Response: ...
    def list_displays(self) -> Response: ...
    def assign_display(self, value: str | None) -> Response: ...
    def clear_display(self, value: str | None) -> Response: ...
    def show_schedule(self) -> Response: ...
    def add_schedule_rule(self, value: str | None) -> Response: ...
    def drop_schedule_rule(self, value: str | None) -> Response: ...
    def list_providers(self) -> Response: ...
    def search(self, value: str | None) -> Outcome: ...
    def download(self, value: str | None) -> Outcome: ...
    def quit(self) -> Response: ...


def build_verb_table(commands: Commands) -> dict[str, Handler]:
    return {
        "next": lambda _: commands.next_wallpaper(),
        "prev": lambda _: commands.previous_wallpaper(),
        "random": lambda _: commands.random_wallpaper(),
        "shuffle": commands.set_shuffle,
        "cycle": commands.set_cycle,
        "cycle-interval": commands.set_cycle_interval,
        "dynamics": commands.set_dynamics,
        "reload-palette": lambda _: commands.reload_palette(),
        "status": lambda _: commands.report_status(),
        "list": commands.list_library,
        "select": commands.select_wallpaper,
        "favourites": lambda _: commands.list_favourites(),
        "favourite": commands.add_favourite,
        "unfavourite": commands.remove_favourite,
        "remove": commands.remove_wallpaper,
        "pairing": commands.show_pairing,
        "still": commands.set_still,
        "palette": commands.set_palette,
        "reset-pairing": commands.reset_pairing,
        "playlists": commands.list_playlists,
        "playlist-new": commands.make_playlist,
        "playlist-delete": commands.drop_playlist,
        "playlist-add": commands.add_to_playlist,
        "playlist-remove": commands.remove_from_playlist,
        "playlist-use": commands.use_playlist,
        "displays": lambda _: commands.list_displays(),
        "display-assign": commands.assign_display,
        "display-clear": commands.clear_display,
        "schedule": lambda _: commands.show_schedule(),
        "schedule-add": commands.add_schedule_rule,
        "schedule-remove": commands.drop_schedule_rule,
        "providers": lambda _: commands.list_providers(),
        "search": commands.search,
        "download": commands.download,
        "quit": lambda _: commands.quit(),
    }


def parse_toggle(value: str | None, current: bool) -> bool:
    """Interpret ``on``/``off``/``toggle`` (and the usual synonyms)."""
    if value is None or value == "toggle":
        return not current
    lowered = value.strip().lower()
    if lowered in ("on", "true", "1", "yes", "enable", "enabled"):
        return True
    if lowered in ("off", "false", "0", "no", "disable", "disabled"):
        return False
    raise ValueError(f"expected on, off or toggle, got {value!r}")


def parse_search(value: str | None) -> tuple[str, str]:
    """Split ``<provider> [query]``.

    The query is everything after the first word, spaces and all, so that
    quoting it is optional at a shell prompt. An empty query is not an error:
    both providers answer it with whatever they are showing today.
    """
    parts = (value or "").split(maxsplit=1)
    if not parts:
        raise ValueError("usage: search <provider> [query]")
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def parse_download(value: str | None) -> tuple[str, str, str]:
    """Split ``<provider> <identifier> [variant]``.

    The variant is MotionBGS's `hd` or `4k`. Left off, the provider takes the
    best it is offered, which is what the browse dialog's button does too.
    """
    parts = (value or "").split()
    if not 2 <= len(parts) <= 3:
        raise ValueError("usage: download <provider> <identifier> [variant]")
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def parse_list(value: str | None) -> tuple[library_filter.Kinds, str]:
    """Split ``[everything|stills|videos|favourites] [query]``.

    The kind comes first and the rest is the query, spaces and all, exactly as
    `parse_search` reads a provider off the front -- so `list videos snow
    village` is one query and needs no quoting. A first word that names no kind
    is a usage error rather than a query, because `list videos` would otherwise
    be ambiguous between the filter and a search for the word.

    The names are `library.filter.Kinds`' own, so the words typed at a socket
    and the choices in the window's dropdown cannot drift apart.
    """
    parts = (value or "").split(maxsplit=1)
    if not parts:
        return library_filter.Kinds.EVERYTHING, ""
    try:
        kinds = library_filter.Kinds(parts[0].strip().lower())
    except ValueError:
        choices = "|".join(choice.value for choice in library_filter.KIND_CHOICES)
        raise ValueError(f"usage: list [{choices}] [query]") from None
    return kinds, parts[1].strip() if len(parts) > 1 else ""


class UnknownWallpaperError(ValueError):
    """A path from outside that names no wallpaper in the library.

    A `ValueError`, because it is a bad argument and reads as one. It carries a
    `kind` as well so that a script can tell it from `manage`'s refusals: "there
    is no such wallpaper" and "that wallpaper is yours and I will not delete it"
    are different things to do something about.
    """

    kind: Final = "not-in-library"


def parse_path(value: str | None, *, verb: str) -> Path:
    """The one absolute path a path verb takes.

    A relative path is refused rather than guessed at. This server's working
    directory is the window's -- wherever the app was launched from, which for
    a session started by a compositor is `/` -- and almost never where the
    person typing `ctl` is standing, so resolving one here would silently name
    a file in a directory they have never seen. `~` is expanded, because a
    shell that was not asked to expand it (``ctl remove '~/a.png'``) is a
    mistake with only one possible meaning.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError(f"usage: {verb} <path>")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{verb} needs an absolute path, and {text!r} is relative")
    return path


def resolve(library: Library, value: str | None, *, verb: str) -> MediaItem:
    """The wallpaper ``value`` names, or a refusal.

    The only way a path from outside this process becomes something the app
    will act on. It is matched against what the scan actually found rather than
    trusted, which is what keeps `remove` from being a verb that unlinks
    whatever string it is handed -- a bug even when the caller is the person
    who owns the files, because the caller may equally be a script with a stale
    path or a typo in it.

    The match is exact. A path that says the same thing a different way
    (`..` in the middle of it, or a route in through a symlinked directory) is
    refused rather than normalised: resolving it would mean deciding that two
    strings name one file, and being wrong about that here costs somebody a
    file. Every path this app prints is one the scan produced, so the way to
    get one right is to copy it out of `list`.
    """
    path = parse_path(value, verb=verb)
    item = library.find(path)
    if item is None:
        raise UnknownWallpaperError(f"not in the library: {path}")
    return item


def parse_pair(value: str | None, *, verb: str) -> tuple[str, str]:
    """Split ``<path> <rest>`` the way the shell handed it over.

    From the right, because the left side is a path and paths contain spaces --
    this machine's own library lives under one. The right side is a palette
    policy or the word `default`, neither of which ever does.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{verb} needs a path and a value")
    path, separator, rest = text.rpartition(" ")
    if not separator or not path.strip() or not rest.strip():
        raise ValueError(f"{verb} needs a path and a value, as: {verb} <path> <value>")
    return path.strip(), rest.strip()


def parse_pair_from_left(value: str | None, *, verb: str) -> tuple[str, str]:
    """Split ``<name> <rest>`` at the first space.

    The mirror of `parse_pair`, and used where the *left* side is the short
    one: a playlist reference followed by a path or an entry id. A playlist
    whose name has a space in it therefore has to be given by id, which is
    what `playlists` prints alongside it.
    """
    text = (value or "").strip()
    head, separator, tail = text.partition(" ")
    if not separator or not head.strip() or not tail.strip():
        raise ValueError(f"{verb} needs two arguments, as: {verb} <playlist> <value>")
    return head.strip(), tail.strip()


def parse_rule(value: str | None) -> tuple[str, dict[str, str]]:
    """`<playlist> [days=sat,sun] [months=12] [from=22:00] [to=06:00]`.

    Keyword arguments rather than positions, because four optional fields in a
    fixed order is a syntax nobody remembers and every one of them is a
    condition somebody may not want. The playlist comes first because it is the
    only part that is never optional.
    """
    words = (value or "").split()
    if not words:
        raise ValueError("schedule-add needs a playlist, as: schedule-add <playlist> [days=...]")
    playlist, *rest = words
    options: dict[str, str] = {}
    for word in rest:
        key, separator, argument = word.partition("=")
        if not separator or key not in ("days", "months", "from", "to"):
            raise ValueError(f"{word!r} is not one of days=, months=, from= or to=")
        options[key] = argument
    return playlist, options


def describe_pairing(item: MediaItem, bundle: pairings.Pairing) -> str:
    """One pairing as rows, in the format the other listings use."""
    lines = [
        f"# {item.name}: {'customized' if bundle.customized else 'default'}",
        "# fields: field, value",
        f"still\t{bundle.still if bundle.still else '-'}",
        f"motion\t{bundle.motion if bundle.motion else '-'}",
        f"palette\t{bundle.palette.encode()}",
        f"mode\t{bundle.palette.mode.value}",
    ]
    if bundle.override_missing:
        lines.append("# the chosen still is not on disk right now, so the default is in use")
    return "\n".join(lines)


def describe_playlists(store: playlists.Store, active: str) -> str:
    """Every playlist as rows, marking whichever is in force."""
    lines = ["# fields: name, entries, active"]
    for playlist in store.all():
        in_force = "yes" if active in (playlist.id, playlist.name) else "no"
        lines.append(f"{_field(playlist.name)}\t{len(playlist)}\t{in_force}")
    return f"# playlists: {len(store)}\n" + "\n".join(lines)


def describe_playlist(playlist: playlists.Playlist, library: Library) -> str:
    """One playlist as rows, in its own order, entry identity first.

    The entry id leads because it is the field the editing verbs take back,
    the same way `search` leads with the identifier `download` wants.
    """
    absent = set(playlist.missing(library.items))
    lines = [
        f"# {playlist.name}: {len(playlist)} entries",
        "# fields: entry, present, path",
    ]
    for entry in playlist.entries:
        here = "no" if entry.source in absent else "yes"
        lines.append(f"{entry.id}\t{here}\t{_field(entry.source)}")
    return "\n".join(lines)


def remove_wallpaper(item: MediaItem, roots: tuple[Path, ...]) -> str:
    """Take one wallpaper away, and say which of the two ways it went.

    Here rather than in `ui.app` because nothing about it needs a toolkit, and
    because it is the one thing this program does that destroys something: it
    belongs where a test can drive it. All the deciding is `library.manage`'s.
    Ownership is re-derived from disk there, the user's own files are refused
    outright, and there is no confirmation on a socket to fall back on -- so
    there is deliberately no way to ask for the other verb, and no flag that
    turns a refusal into an unlink.

    The sentence has to say which happened. "Removed" and "moved to the trash"
    are the same word to a user in a hurry, and only one of them is
    recoverable.
    """
    if item.deletable:
        result = manage.remove(item, roots)
        return f"{result.describe()} - deleted, which cannot be undone"
    landed = manage.trash(item.path, roots)
    return f"{item.path.name} moved to the trash - {landed}"


# -- rendering for a terminal --------------------------------------------

#: Rows are one line each with tab-separated fields, because that is the format
#: both readers of this output already understand: a person sees columns, and
#: `cut -f1`, `awk -F'\t'` and `while read -r id kind rest` all get the fields
#: out with nothing installed. Tabs rather than spaces because a wallpaper title
#: is full of spaces and would otherwise read as several columns.
SEPARATOR: Final = "\t"

#: Everything that is not a row -- the summary, the column names -- is a comment
#: line, which is the one convention those same tools already know how to skip.
COMMENT: Final = "# "

#: Stands in for a field the provider left empty, so a column is never
#: invisible.
BLANK: Final = "-"

#: How many bytes of rows one listing may spend. A library is six hundred
#: wallpapers on this machine and the whole reply has to fit in one frame, so
#: unlike a page of search results it can genuinely run out of room; saying so
#: in the summary is the alternative to `Response.encode` refusing the lot and
#: the caller getting a size error instead of their wallpapers. Half the frame,
#: because these rows are JSON-escaped into it afterwards and the escaping only
#: ever grows them.
LIST_BUDGET: Final = MAX_MESSAGE_BYTES // 2


def _field(text: str) -> str:
    """One column's worth of text from an untrusted title.

    Tabs separate the columns and newlines separate the rows, so neither may
    survive inside a string a website chose; a title carrying either would
    otherwise invent columns or whole rows in a script's input.
    """
    collapsed = " ".join(text.split())
    return collapsed or BLANK


def render_providers(infos: Sequence[registry.ProviderInfo]) -> str:
    """The provider list, with what each one cannot currently do."""
    lines = [f"{COMMENT}fields: name, media, usable, limitations"]
    lines.extend(
        SEPARATOR.join(
            (
                _field(info.name),
                _field(info.media_kind.value),
                "yes" if info.usable else "no",
                # Semicolons rather than a row each: the name in column one has
                # to stay the key of the line it is on.
                _field("; ".join(info.limitations)),
            )
        )
        for info in infos
    )
    return "\n".join(lines)


def summarise(result: SearchResult) -> str:
    """The same sentence the browse dialog puts under its grid.

    Deliberately word-for-word: `dropped` counts results the provider returned
    that we refused to normalise, and a sudden jump in it means the remote's
    markup moved. That is worth noticing from a script too.
    """
    parts = [f"{len(result)} result{'' if len(result) == 1 else 's'}"]
    if result.total_hint:
        parts.append(f"of about {result.total_hint}")
    parts.append(f"page {result.page}")
    if result.dropped:
        parts.append(f"{result.dropped} unreadable")
    if result.cached:
        parts.append("cached")
    return " - ".join(parts)


def render_search(result: SearchResult) -> str:
    """One page of results: a summary, the column names, then a row each.

    The identifier comes first because it is the field with a use -- it is what
    `download` takes back.
    """
    lines = [
        f"{COMMENT}{result.provider}: {summarise(result)}",
        f"{COMMENT}fields: identifier, kind, resolution, title",
    ]
    lines.extend(
        SEPARATOR.join(
            (
                _field(item.identifier),
                _field(item.kind.value),
                _field(item.resolution),
                _field(item.title or item.identifier),
            )
        )
        for item in result.items
    )
    return "\n".join(lines)


def _path_field(path: Path) -> str | None:
    """``path`` as a column, or ``None`` if it cannot honestly be one.

    Unlike a provider's title, a path may not be tidied on the way out: it is
    the field with a use here -- what `select`, `favourite` and `remove` take
    back -- so collapsing the whitespace inside it, as `_field` does, would
    print a key that no longer names the file. Tabs and newlines are this
    format's structure and cannot be printed at all, so the rare wallpaper
    carrying one in its name is counted in the summary rather than shown under
    a name that would not work.
    """
    text = str(path)
    return None if any(character in text for character in "\t\n\r") else text


def _within_budget(rows: Sequence[str]) -> tuple[list[str], int]:
    """As many rows as fit in one reply, and how many did not.

    Truncating in the middle of a row would hand a script half a path, so the
    cut is always between two of them.
    """
    kept: list[str] = []
    spent = 0
    for index, row in enumerate(rows):
        spent += len(row.encode(ENCODING)) + 1
        if spent > LIST_BUDGET:
            return kept, len(rows) - index
        kept.append(row)
    return kept, 0


def render_library(
    items: Sequence[MediaItem],
    query: library_filter.Query,
    favourites: Collection[Path],
) -> str:
    """What the library holds, in the rows `search` already writes.

    The selecting and the ordering are `library.filter`'s -- the grid's own --
    rather than a second matcher written for the socket, so `ctl list videos
    snow vil` and the window's search box can never disagree about which
    wallpapers those words name.

    `ownership` is in the columns because it is the field that says what
    `remove` would do to that file: `managed` is deleted, `user` is moved to
    the trash, and those are not the same decision.
    """
    selected = library_filter.apply(items, query, favourites)
    starred = frozenset(favourites)
    rows: list[str] = []
    unlistable = 0
    for item in selected:
        printed = _path_field(item.path)
        if printed is None:
            unlistable += 1
            continue
        rows.append(
            SEPARATOR.join(
                (
                    printed,
                    _field(item.kind.value),
                    _field(item.ownership.value),
                    "yes" if item.path in starred else "no",
                )
            )
        )

    kept, dropped = _within_budget(rows)
    parts = [f"{len(kept)} of {len(items)} {library_filter.describe(query)}"]
    if dropped:
        parts.append(f"{dropped} more than fit in one reply")
    if unlistable:
        parts.append(f"{unlistable} unlistable")
    return "\n".join(
        [
            f"{COMMENT}library: {' - '.join(parts)}",
            f"{COMMENT}fields: path, kind, ownership, favourite",
            *kept,
        ]
    )


def render_favourites(entries: Sequence[Path], present: Collection[Path]) -> str:
    """The starred list itself, in the order the user built it.

    Not the same question as `list favourites`, which can only show what the
    last scan found. A favourite on a drive that is not mounted is still a
    favourite -- `library.favourites` keeps it on purpose -- and this is where
    a caller can see that it is still there rather than concluding the app
    forgot it. That is what the `present` column is for.
    """
    known = frozenset(present)
    rows: list[str] = []
    unlistable = 0
    for entry in entries:
        printed = _path_field(entry)
        if printed is None:
            unlistable += 1
            continue
        rows.append(SEPARATOR.join((printed, "yes" if entry in known else "no")))

    kept, dropped = _within_budget(rows)
    missing = sum(1 for entry in entries if entry not in known)
    parts = [f"{len(entries)} starred"]
    if missing:
        parts.append(f"{missing} not in the library right now")
    if dropped:
        parts.append(f"{dropped} more than fit in one reply")
    if unlistable:
        parts.append(f"{unlistable} unlistable")
    return "\n".join(
        [
            f"{COMMENT}favourites: {' - '.join(parts)}",
            f"{COMMENT}fields: path, present",
            *kept,
        ]
    )


# -- dispatch ------------------------------------------------------------


def _kind_of(error: Exception) -> str:
    """The machine-readable half of an error that has one.

    Three of this app's exceptions are the same shape: `ProviderError`,
    `ManageError` and `FavouritesError` each pair a `kind` with a sentence
    that already reads ``kind: message``. None of them share a base class,
    because they belong to three subsystems with nothing else in common, so
    the shape is the contract and asking for the attribute is how this module
    relays all three without importing every one of them.
    """
    kind = getattr(error, "kind", "")
    return kind if isinstance(kind, str) else ""


def failed(error: Exception) -> Response:
    """One failed response for any exception, keeping the reason where there is one.

    The `kind` is the machine-readable half the browse dialog branches on, and
    a caller holding a socket deserves the same: `rate-limit` from a provider,
    `not-ours` from a removal that will not touch the user's own file. The
    accompanying `str` already reads ``kind: message``, which is the sentence
    the dialog toasts, so the client prints one thing and switches on another.
    """
    kind = _kind_of(error)
    if kind:
        return Response.failure(str(error), kind=kind)
    if isinstance(error, ValueError):
        # Argument validation. The message is the whole point of it.
        return Response.failure(str(error))
    # Anything else is a bug or a broken machine, and the type name is the only
    # part of it a person can act on.
    return Response.failure(f"{type(error).__name__}: {error}")


def handle(verbs: dict[str, Handler], line: bytes) -> Outcome:
    """Decode one request line and run it. Never raises."""
    try:
        request = Request.decode(line)
    except ProtocolError as error:
        return Response.failure(str(error))

    handler = verbs.get(request.verb)
    if handler is None:
        known = ", ".join(sorted(verbs))
        return Response.failure(f"unknown verb {request.verb!r}; known verbs: {known}")

    try:
        return handler(request.argument)
    except Exception as error:
        # Broad on purpose: a handler must never take the app down, and an
        # unreachable network raises whatever the transport underneath felt like.
        return failed(error)


def dispatch(verbs: dict[str, Handler], line: bytes, reply: Reply) -> None:
    """Run one request and hand its answer to ``reply``, exactly once.

    ``reply`` runs before this returns for every verb that can answer at once,
    and some time later for the ones that go to the network. Guarding the
    once-ness here rather than trusting each handler keeps a double reply -- two
    responses down a connection framed one-per-line -- impossible by
    construction.
    """
    outcome = handle(verbs, line)
    if isinstance(outcome, Response):
        reply(outcome)
        return

    answered = False

    def once(response: Response) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        reply(response)

    try:
        outcome.start(once)
    except Exception as error:
        # A deferral that fails to even start still owes the client an answer.
        once(failed(error))


#: AF_UNIX paths are capped near 108 bytes. Past that, `Gio.SocketService`
#: silently fails to create the socket -- `add_address` returns without
#: complaint and nothing is listening -- so we refuse the address rather than
#: pretend we have one. The same ceiling, for the same reason, as
#: `wallpaper.renderer.MAX_SOCKET_PATH_BYTES`, which found it first with mpv.
MAX_SOCKET_PATH_BYTES: Final = 100


class SocketServer:
    """Binds the control socket and answers requests from the GTK main loop."""

    #: Backlog is small on purpose: clients are one-shot `ctl` invocations.
    BACKLOG: Final = 8

    def __init__(self, commands: Commands, path: Path | None = None) -> None:
        self._verbs = build_verb_table(commands)
        self._path = path if path is not None else paths.socket_path()
        self._service: object | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _clear_stale_socket(self) -> None:
        """Remove a socket left behind by a crashed instance.

        Only when nothing answers on it -- a live socket means another instance
        is running and we must not steal its address.
        """
        if not self._path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self._path))
        except (ConnectionRefusedError, FileNotFoundError):
            self._path.unlink(missing_ok=True)
            return
        except OSError:
            self._path.unlink(missing_ok=True)
            return
        else:
            raise RuntimeError(f"another instance is already listening on {self._path}")
        finally:
            probe.close()

    def start(self) -> None:
        """Listen, or raise `RuntimeError` saying why not.

        Every failure leaves as `RuntimeError` and nothing else, because that
        is the contract the caller relies on: losing the control socket costs
        the Noctalia plugin's buttons, not the app, and `ui.app` catches this
        one type and carries on into the window. An `OSError` escaping from
        here instead is a wallpaper manager that will not start at all --
        which is exactly what an over-long runtime directory used to do.
        """
        # Before anything is created, since past the ceiling nothing would be.
        if len(os.fsencode(self._path)) > MAX_SOCKET_PATH_BYTES:
            raise RuntimeError(
                f"{self._path} is too long for a unix socket "
                f"({MAX_SOCKET_PATH_BYTES} bytes at most)"
            )

        from gi.repository import Gio, GLib

        try:
            paths.ensure_directory(self._path.parent)
        except OSError as error:
            raise RuntimeError(
                f"cannot create {self._path.parent}: {error.strerror or error}"
            ) from error
        self._clear_stale_socket()

        service = Gio.SocketService.new()
        address = Gio.UnixSocketAddress.new(str(self._path))
        try:
            service.add_address(
                address,
                Gio.SocketType.STREAM,
                Gio.SocketProtocol.DEFAULT,
                None,
            )
        except GLib.Error as error:
            raise RuntimeError(f"cannot bind {self._path}: {error.message}") from error

        service.connect("incoming", self._on_incoming)
        service.start()
        self._service = service
        try:
            # The socket carries control of the wallpaper; no reason for anyone
            # else on the system to reach it.
            os.chmod(self._path, 0o600)
        except OSError as error:
            # A service is already listening on a socket we could not secure.
            # Stop it and take the address back down: half-started is worse
            # than not started, since the reply would be that we have no
            # control socket while one sat there readable by the machine.
            self.stop()
            raise RuntimeError(f"cannot secure {self._path}: {error.strerror or error}") from error

    def stop(self) -> None:
        service = self._service
        if service is not None:
            service.stop()  # type: ignore[attr-defined]
            self._service = None
        self._path.unlink(missing_ok=True)

    def _on_incoming(self, _service: object, connection: object, _source: object) -> bool:
        from gi.repository import Gio

        assert isinstance(connection, Gio.SocketConnection)
        stream = Gio.DataInputStream.new(connection.get_input_stream())
        stream.set_close_base_stream(True)
        stream.read_line_async(0, None, self._on_line, connection)
        # True keeps the connection alive past this callback; the async read
        # owns it from here.
        return True

    def _on_line(self, stream: object, result: object, connection: object) -> None:
        from gi.repository import Gio, GLib

        assert isinstance(stream, Gio.DataInputStream)
        assert isinstance(connection, Gio.SocketConnection)
        try:
            line, _length = stream.read_line_finish(result)  # type: ignore[arg-type]
        except GLib.Error:
            line = None

        if line is None:
            self._answer(connection, Response.failure("empty request"))
            return
        # `dispatch` writes the reply itself, which for a search or a download
        # happens once the worker running it has come back to this thread. The
        # connection is held open in the meantime and closed by `_answer`.
        dispatch(self._verbs, bytes(line), lambda response: self._answer(connection, response))

    def _answer(self, connection: object, response: Response) -> None:
        from gi.repository import Gio, GLib

        assert isinstance(connection, Gio.SocketConnection)
        try:
            payload = response.encode()
        except ProtocolError as error:
            # A reply too large for the frame: say so rather than send half of
            # it, which the client would read as a malformed message.
            payload = Response.failure(f"reply could not be sent: {error}").encode()
        try:
            connection.get_output_stream().write_all(payload, None)
        except GLib.Error:
            # The client hung up before reading; nothing useful to do.
            pass
        finally:
            with contextlib.suppress(GLib.Error):
                connection.close(None)
