"""The real `PlaylistEditing`: the playlist store, written on the authoring lane.

**One task per change.** Every write is one task on the application's
ordered authoring lane (`Application.authoring_action_async`): its checks
run on GTK when it reaches the head of the queue, against what is saved by
then, and its store transaction runs on the lane's worker. Adding several
wallpapers (the picker, a drop) and removing several entries are one
transaction each (`playlists.Store.insert_entries`, `remove_entries`), all
of them or none. After a save the application republishes the runtime
configuration and hands the window the session
(`Application.playlists_changed`), which reloads the adapter: the page shows
what was saved. A refusal or a failure is reported ("…; nothing changed")
and the page goes back to what is saved (``playlist-edits``).

**Moves are relative.** A keyboard move or a drag is "this entry, n places",
applied to the entry's position when the move reaches the head of the queue
(`playlists.Store.move_entry_relative`), as the classic page does: queued
gestures apply in order, each to the order the previous one saved, and a
move that fails invents nothing for the ones after it. A move that changes
nothing (the first entry moved up) writes and publishes nothing.

**What is shown meanwhile.** A rename, an interval, a shuffle and the
default show the latest choice until every write for it has settled, then
what is saved (`wall_in_one.ui.pending_edits`). Entries are always the saved
ones; `PlaylistDetail.busy` says that changes to them are still queued.

**Nothing playable.** A playlist that a schedule rule, the schedule's
default or a display uses must keep something to play: the runtime
configuration cannot be built otherwise (``runtime_config._compile``), and
the service would keep the previous one. So removing the last entry it can
play is refused, and so is making a playlist with nothing to play the
default. Both are checked at the head of the queue, against the saved
rules, assignments and default, plus a default this adapter asked for that
has not settled yet.

**Hidden playlists.** "Your pick" is the ``quick-choice`` playlist and its
per-display ``quick-choice:<hash>`` siblings; "All wallpapers" is generated
from the library. None of them is edited, renamed or deleted here; "All
wallpapers" can only be duplicated.

**Undo.** Removing entries offers Undo once the removal is saved. Undo is a
task on the same lane that puts the same entries (their own ids) back at
their old positions, and only while the playlist is exactly as the removal
left it; otherwise it says why and changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from wall_in_one import config, runtime_config
from wall_in_one.library import pairings, playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.library.schedules import Rule
from wall_in_one.session import QUICK_CHOICE_ID, Session
from wall_in_one.ui.next.catalog import quoted
from wall_in_one.ui.next.playlists_state import (
    EDITS_TOPIC,
    EntryView,
    PlaylistDetail,
    Rotation,
    Usage,
    usage_words,
)
from wall_in_one.ui.next.state import Undo
from wall_in_one.ui.pending_edits import queue_intent, settle_intent
from wall_in_one.ui.status_model import RuntimeStatusModel

if TYPE_CHECKING:
    from wall_in_one.ui.next.real_state import LibraryWallpaper

_T = TypeVar("_T")

#: How a display came to show a playlist, in the page's words.
_ROUTE_WORDS: Final = {
    "manual": "your pick",
    "schedule": "from schedule",
    "assignment": "this display\u2019s playlist",
    "default": "nothing scheduled",
}
_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_DEFAULT: Final = "default"


class PlaylistBackend(Protocol):
    """What the playlist actions use of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def settings(self) -> config.Settings: ...

    @property
    def status_model(self) -> RuntimeStatusModel: ...

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool: ...

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem: ...

    def playlists_changed(self) -> None: ...

    def delete_playlist_async(
        self,
        reference: str,
        on_complete: Callable[[Any, tuple[str, ...]], None],
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> bool: ...

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool: ...

    def activate_playlist_async(self, reference: str) -> bool: ...

    def window_report(self, message: str) -> None: ...


class Shown(Protocol):
    """What the actions read of the adapter: the library as it shows it, and its signals."""

    def library_entry(self, wid: str) -> LibraryWallpaper | None: ...

    def toast(
        self,
        text: str,
        undo: Undo | None = None,
        action: tuple[str, Callable[[], None]] | None = None,
    ) -> None: ...

    def emit_changed(self, *topics: str) -> None: ...


class RefusedError(ValueError):
    """A change the checks at the head of the queue turned down; its text says why."""


def hidden(pid: str) -> bool:
    """ "Your pick" (one global, one per display) and "All wallpapers": never edited here."""
    return (
        pid == QUICK_CHOICE_ID
        or pid.startswith(playlists.DISPLAY_QUICK_CHOICE_ID_PREFIX)
        or pid == runtime_config.FALLBACK_PLAYLIST_ID
    )


@dataclass(frozen=True, slots=True)
class Playability:
    """What one entry offers playback: there at all, and playable."""

    present: bool
    usable: bool


def leaves_nothing_to_play(before: Sequence[Playability], after: Sequence[Playability]) -> bool:
    """Whether a removal takes away the last thing a used playlist can play.

    Refused when nothing usable would remain where something did, and also
    when nothing at all would remain that the library has: a used playlist
    with no entries in the library cannot be compiled. A playlist that
    already had nothing usable may still lose skipped entries while one the
    library has remains, so it can be cleaned up.
    """
    if any(entry.usable for entry in after):
        return False
    return any(entry.usable for entry in before) or not any(entry.present for entry in after)


def rule_words(rule: Rule) -> str:
    """A rule in words: ``Weekdays 07:00\u201318:00``, ``Weekends, all day``, ``All of Dec``.

    A rule aimed at one display adds it: ``Every day 07:00\u201318:00 · DP-1``.
    """
    days = sorted(rule.weekdays)
    if not days or len(days) == 7:
        day_text = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        day_text = "Weekdays"
    elif days == [5, 6]:
        day_text = "Weekends"
    elif len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        day_text = f"{_DAYS[days[0]]}\u2013{_DAYS[days[-1]]}"
    else:
        day_text = ", ".join(_DAYS[day] for day in days)
    timed = rule.start is not None and rule.end is not None and rule.start != rule.end
    if timed:
        assert rule.start is not None and rule.end is not None
        start = f"{rule.start // 60:02d}:{rule.start % 60:02d}"
        end = f"{rule.end // 60:02d}:{rule.end % 60:02d}"
        text = f"{day_text} {start}\u2013{end}"
    elif days and len(days) != 7:
        text = f"{day_text}, all day"
    elif rule.months:
        text = ""
    else:
        text = "Always"
    if rule.months:
        months = sorted(rule.months)
        if len(months) == 1:
            month_text = _MONTHS[months[0] - 1]
        elif months == list(range(months[0], months[-1] + 1)):
            month_text = f"{_MONTHS[months[0] - 1]}\u2013{_MONTHS[months[-1] - 1]}"
        else:
            month_text = ", ".join(_MONTHS[month - 1] for month in months)
        text = f"{text} in {month_text}" if text else f"All of {month_text}"
    if rule.connector:
        text += f" · {rule.connector}"
    return text


def copy_name(name: str, taken: Iterable[str]) -> str:
    """``Evening (copy)``, then ``Evening (copy 2)``…, unused by any other playlist."""
    used = {other.casefold() for other in taken}
    candidate, number = f"{name} (copy)", 2
    while candidate.casefold() in used:
        candidate, number = f"{name} (copy {number})", number + 1
    return candidate


class RealPlaylists:
    """`PlaylistEditing` over the application's playlist store, for a `RealAppState`."""

    def __init__(
        self,
        backend: PlaylistBackend,
        shown: Shown,
        files_blocked: Callable[[str], str],
    ) -> None:
        self._backend = backend
        self._shown = shown
        #: Why ``what`` is off because of files from a newer version, or empty.
        self._files_blocked = files_blocked
        self._creating: set[str] = set()
        #: Entry writes still queued, per playlist.
        self._entry_writes: dict[str, int] = {}
        # What each control shows while its writes are queued (`pending_edits`).
        self._names: dict[str, tuple[str, int]] = {}
        self._intervals: dict[str, tuple[int | None, int]] = {}
        self._shuffles: dict[str, tuple[bool | None, int]] = {}
        self._defaults: dict[str, tuple[str, int]] = {}
        #: Every playlist asked to be the default and not settled, with a count.
        self._default_asked: dict[str, int] = {}

    @property
    def _session(self) -> Session:
        return self._backend.session

    @property
    def _store(self) -> playlists.Store:
        return self._session.playlists

    # -- reading ------------------------------------------------------------------
    def editing_blocked(self) -> str:
        return self._files_blocked("Playlist changes")

    def creating(self, pid: str) -> bool:
        return pid in self._creating

    def _editable(self, pid: str) -> playlists.Playlist | None:
        return None if hidden(pid) else self._store.get(pid)

    def _entry_view(self, entry: playlists.Entry) -> EntryView:
        view = self._shown.library_entry(entry.source)
        if view is None:
            return EntryView(entry.id, entry.source, Path(entry.source).name, missing=True)
        return EntryView(entry.id, entry.source, view.name, problem=view.problem)

    def _playability(self, entries: Iterable[playlists.Entry]) -> list[Playability]:
        found: list[Playability] = []
        for entry in entries:
            view = self._shown.library_entry(entry.source)
            found.append(Playability(view is not None, view is not None and not view.problem))
        return found

    def _default_pid(self) -> str:
        pending = self._defaults.get(_DEFAULT)
        return pending[0] if pending is not None else self._backend.settings.active_playlist

    def detail(self, pid: str) -> PlaylistDetail | None:
        playlist = self._editable(pid)
        if playlist is None:
            return None
        entries = tuple(self._entry_view(entry) for entry in playlist.entries)
        usable = sum(entry.usable for entry in entries)
        name = self._names[pid][0] if pid in self._names else playlist.name
        blocked = self.editing_blocked()
        if blocked:
            play_blocked = default_blocked = blocked
        elif not entries:
            play_blocked = default_blocked = "Add wallpapers to it first"
        elif not usable:
            play_blocked = default_blocked = (
                "Every wallpaper in it is missing or skipped; add one that plays first"
            )
        else:
            play_blocked = default_blocked = ""
        on_screen, playing, showing = self._on_screen(pid)
        settings = self._backend.settings
        interval = self._intervals[pid][0] if pid in self._intervals else playlist.cycle_interval
        shuffle = self._shuffles[pid][0] if pid in self._shuffles else playlist.shuffle
        return PlaylistDetail(
            id=playlist.id,
            name=name,
            entries=entries,
            usage=self._usage(pid),
            on_screen=on_screen,
            playing=playing,
            showing=showing,
            play_blocked=play_blocked,
            is_default=self._default_pid() == pid,
            default_blocked=default_blocked,
            rotation=Rotation(
                interval=interval,
                shuffle=shuffle,
                default_interval=settings.cycle_interval,
                default_shuffle=settings.shuffle,
                note=self._rotation_note(),
            ),
            busy=self._entry_writes.get(pid, 0) > 0,
        )

    def _usage(self, pid: str) -> tuple[Usage, ...]:
        found: list[Usage] = []
        if self._default_pid() == pid:
            found.append(
                Usage(
                    "default",
                    "When nothing is scheduled",
                    "Plays whenever no schedule rule applies",
                )
            )
        for rule in self._session.schedules.rules:
            if rule.playlist != pid:
                continue
            found.append(
                Usage(
                    "rule",
                    rule.name or rule_words(rule),
                    "Schedule rule \u2014 open the Schedule"
                    if rule.enabled
                    else "This rule is turned off \u2014 open the Schedule",
                    off=not rule.enabled,
                )
            )
        mirrored = self._backend.settings.display_mode != config.DISPLAY_MODE_INDEPENDENT
        for connector, assigned in self._session.displays.all():
            if assigned != pid:
                continue
            tooltip = f"{connector} plays this playlist when no rule applies to it"
            if mirrored:
                tooltip += ", once displays are controlled on their own"
            found.append(
                Usage("display", connector, tooltip + " \u2014 open Displays", target="displays")
            )
        return tuple(found)

    def _on_screen(self, pid: str) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
        """Where the runtime shows it now and why, whether that is a pick
        everywhere, and which entry each of those displays shows."""
        view = self._backend.status_model.view
        truth = view.truth if view.service == "running" else None
        if truth is None:
            return "", False, ()
        rows = [display for display in truth.displays if display.connected]
        if not rows:
            # A version-1 runtime: one answer for every display.
            if truth.playlist_id != pid:
                return "", False, ()
            why = "your pick" if truth.is_manual else "from schedule"
            return f"On screen now · {why}", truth.is_manual, ()
        showing = [display for display in rows if display.playlist_id == pid]
        if not showing:
            return "", False, ()
        everywhere = len(showing) == len(rows)
        where = (
            "On screen now"
            if everywhere
            else f"On {' and '.join(display.connector for display in showing)} now"
        )
        routes = {display.route_source for display in showing}
        why = _ROUTE_WORDS.get(next(iter(routes)), "") if len(routes) == 1 else ""
        playing = everywhere and routes == {"manual"}
        entries = tuple(
            (display.entry_id, display.connector) for display in showing if display.entry_id
        )
        return (f"{where} · {why}" if why else where), playing, entries

    def _rotation_note(self) -> str:
        view = self._backend.status_model.view
        parts: list[str] = []
        if not self._backend.settings.cycle_enabled:
            parts.append(
                "The interval counts while \u201cChange wallpaper automatically\u201d is on."
            )
        if (
            view.service == "running"
            and view.status is not None
            and not runtime_config.runtime_applies_overrides(view.status)
        ):
            parts.append(
                "The wallpaper service running now predates these; they apply once it restarts."
            )
        return " ".join(parts)

    def _references(self, pid: str) -> tuple[Usage, ...]:
        """Every saved use of ``pid`` (rules on or off), and any default asked for.

        The default counts while it is saved *or* asked for and not settled:
        a removal queued behind a default that then fails must still find it.
        """
        used = self._usage(pid)
        asked = self._backend.settings.active_playlist == pid or pid in self._default_asked
        if asked and not any(usage.kind == "default" for usage in used):
            used = (Usage("default", "When nothing is scheduled"), *used)
        return used

    # -- the lane ---------------------------------------------------------------------
    def _emit_edits(self) -> None:
        self._shown.emit_changed(EDITS_TOPIC)

    def _queue(
        self,
        what: str,
        prepare: Callable[[], Callable[[], _T]],
        saved: Callable[[_T], bool],
        *,
        settle: Callable[[], object] | None = None,
    ) -> bool:
        """One task on the authoring lane; ``saved`` says whether the store changed.

        ``settle`` runs exactly once, whatever happens: saved, refused at the
        head of the queue, failed, or not queued at all.
        """
        settled = False

        def done() -> None:
            nonlocal settled
            if not settled:
                settled = True
                if settle is not None:
                    settle()

        blocked = self.editing_blocked()
        if blocked:
            done()
            self._backend.window_report(blocked)
            self._emit_edits()
            return False

        def finish(result: _T) -> None:
            done()
            if saved(result):
                # Republishes, and hands the window the session (which reloads us).
                self._backend.playlists_changed()
            else:
                self._emit_edits()

        def failed(message: str) -> None:
            done()
            self._backend.window_report(f"{what}; nothing changed: {message}")
            self._emit_edits()

        accepted = self._backend.authoring_action_async(
            lambda: prepare()(), finish, prepare=prepare, failure=failed
        )
        if not accepted and not settled:
            # Refused without a word (shutting down, or authoring not open
            # yet, which says so itself): nothing is queued.
            done()
            self._emit_edits()
        return accepted

    def _entry_write(self, pid: str) -> Callable[[], None]:
        """Count one more queued entry write for ``pid``; the returned call settles it."""
        self._entry_writes[pid] = self._entry_writes.get(pid, 0) + 1

        def settle() -> None:
            left = self._entry_writes.get(pid, 0) - 1
            if left > 0:
                self._entry_writes[pid] = left
            else:
                self._entry_writes.pop(pid, None)

        return settle

    def _require(self, pid: str, what: str) -> playlists.Playlist:
        """The saved playlist at the head of the queue, or a refusal."""
        playlist = self._store.get(pid)
        if playlist is None:
            raise RefusedError(f"it was deleted before {what} could be saved")
        return playlist

    # -- playlists ----------------------------------------------------------------------
    def create_playlist(self, name: str) -> str:
        pid = playlists.new_id()
        store = self._store
        self._creating.add(pid)

        def prepare() -> Callable[[], playlists.Playlist]:
            return lambda: store.create(name, entry_id=pid)

        self._queue(
            f"{quoted(name.strip() or name)} was not created",
            prepare,
            lambda _made: True,
            settle=lambda: self._creating.discard(pid),
        )
        return pid

    def duplicate_playlist(self, pid: str) -> str:
        if pid != runtime_config.FALLBACK_PLAYLIST_ID and self._editable(pid) is None:
            return ""
        copy = playlists.new_id()
        store = self._store
        session = self._session
        self._creating.add(copy)

        def prepare() -> Callable[[], playlists.Playlist]:
            if pid == runtime_config.FALLBACK_PLAYLIST_ID:
                source_name = "All wallpapers"
                sources = [str(item.path) for item in session.library.items]
                interval: int | None = None
                shuffle: bool | None = None
            else:
                source = self._require(pid, "the copy")
                source_name = source.name
                sources = [entry.source for entry in source.entries]
                interval, shuffle = source.cycle_interval, source.shuffle
            name = copy_name(source_name, (playlist.name for playlist in store.all()))
            placed = [
                (index, playlists.Entry(playlists.new_id(), source_path))
                for index, source_path in enumerate(sources)
            ]

            def work() -> playlists.Playlist:
                made = store.create(name, entry_id=copy)
                try:
                    if placed:
                        made = store.insert_entries(copy, placed)
                    if interval is not None or shuffle is not None:
                        made = store.set_rotation(copy, cycle_interval=interval, shuffle=shuffle)
                except playlists.PlaylistError as error:
                    try:
                        store.delete(copy)  # no half copy
                    except playlists.PlaylistError as cleanup:
                        raise playlists.PlaylistError(
                            error.kind,
                            f"{error}; the unfinished copy {name!r} could not be removed: "
                            f"{cleanup}",
                        ) from error
                    raise
                return made

            return work

        self._queue(
            "The copy was not made",
            prepare,
            lambda _made: True,
            settle=lambda: self._creating.discard(copy),
        )
        return copy

    def rename_playlist(self, pid: str, name: str) -> None:
        playlist = self._editable(pid)
        if playlist is None:
            return
        tidy = " ".join(name.split())
        shown = self._names[pid][0] if pid in self._names else playlist.name
        if tidy == shown:
            return
        store = self._store
        queue_intent(self._names, pid, tidy)
        self._emit_edits()

        def prepare() -> Callable[[], tuple[playlists.Playlist, playlists.Playlist]]:
            before = self._require(pid, "the new name")
            return lambda: (before, store.rename(pid, name))

        def saved(result: tuple[playlists.Playlist, playlists.Playlist]) -> bool:
            before, renamed = result
            return renamed != before

        self._queue(
            f"{quoted(playlist.name)} was not renamed",
            prepare,
            saved,
            settle=lambda: settle_intent(self._names, pid),
        )

    def delete_warning(self, pid: str) -> str:
        playlist = self._editable(pid)
        if playlist is None:
            return ""
        session = self._session
        count = len(playlist.entries)
        parts = [f"{count} playlist entr{'y' if count == 1 else 'ies'}"]
        rules = sum(rule.playlist == pid for rule in session.schedules.rules)
        if rules:
            parts.append(f"{rules} schedule rule{'' if rules == 1 else 's'}")
        assigned = sum(playlist_id == pid for _connector, playlist_id in session.displays.all())
        if assigned:
            parts.append(f"{assigned} display assignment{'' if assigned == 1 else 's'}")
        if self._backend.settings.active_playlist == pid:
            parts.append("the saved default selection")
        if session.manual_playlist == pid:
            parts.append("the current manual override")
        listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
        return f"This removes {listed}. Wallpaper files stay in the library. This cannot be undone."

    def delete_playlist(self, pid: str) -> None:
        playlist = self._editable(pid)
        if playlist is None:
            return
        blocked = self.editing_blocked()
        if blocked:
            self._backend.window_report(blocked)
            return
        name = playlist.name

        def deleted(_result: object, failures: tuple[str, ...]) -> None:
            if failures:
                self._backend.window_report(
                    f"{quoted(name)} was deleted, but cleanup needs attention: "
                    + "; ".join(failures)
                )
            else:
                self._shown.toast(f"Deleted {quoted(name)}")

        def failed(error: str) -> None:
            self._backend.window_report(f"{quoted(name)} was not deleted; nothing changed: {error}")
            self._emit_edits()

        self._backend.delete_playlist_async(pid, deleted, on_error=failed)

    # -- entries --------------------------------------------------------------------------
    def add_entries(self, pid: str, wids: Sequence[str], position: int | None = None) -> None:
        playlist = self._editable(pid)
        views = [view for wid in wids if (view := self._shown.library_entry(wid)) is not None]
        if playlist is None or not views:
            return
        for view in views:
            if view.problem:
                self._backend.window_report(
                    f"Playback unavailable for {view.name}; it can\u2019t be added: {view.problem}"
                )
                return
        backend = self._backend
        store = self._store
        name = playlist.name

        def prepare() -> Callable[[], playlists.Playlist]:
            current = self._require(pid, "the wallpapers")
            items = [backend.current_item_for_authoring(view.item) for view in views]
            for item in items:
                health = self._session.pairings.health(pairings.Identity.of(item))
                if health.is_borked:
                    raise RefusedError(
                        f"playback became unavailable for {item.name}; "
                        f"it can\u2019t be added: {health.reason}"
                    )
            at = len(current.entries) if position is None else position
            placed = [
                (at + index, playlists.Entry(playlists.new_id(), str(item.path)))
                for index, item in enumerate(items)
            ]
            return lambda: store.insert_entries(pid, placed)

        def saved(_updated: playlists.Playlist) -> bool:
            what = quoted(views[0].name) if len(views) == 1 else f"{len(views)} wallpapers"
            self._shown.toast(f"Added {what} to {quoted(name)}")
            return True

        self._queue(
            f"{quoted(views[0].name) if len(views) == 1 else f'{len(views)} wallpapers'} "
            f"{'was' if len(views) == 1 else 'were'} not added to {quoted(name)}",
            prepare,
            saved,
            settle=self._entry_write(pid),
        )
        self._emit_edits()

    def remove_entries(self, pid: str, entry_ids: Sequence[str]) -> None:
        playlist = self._editable(pid)
        if playlist is None or not entry_ids:
            return
        chosen = frozenset(entry_ids)
        store = self._store
        name = playlist.name
        names = {entry.id: self._entry_view(entry).name for entry in playlist.entries}
        what = (
            quoted(names.get(next(iter(chosen)), "the wallpaper"))
            if len(chosen) == 1
            else f"{len(chosen)} wallpapers"
        )

        def prepare() -> Callable[
            [], tuple[playlists.Playlist, tuple[tuple[int, playlists.Entry], ...]]
        ]:
            current = self._require(pid, "the removal")
            removed = tuple(
                (index, entry) for index, entry in enumerate(current.entries) if entry.id in chosen
            )
            kept = [entry for entry in current.entries if entry.id not in chosen]
            used = self._references(pid)
            if used and leaves_nothing_to_play(
                self._playability(current.entries), self._playability(kept)
            ):
                raise RefusedError(
                    f"{quoted(current.name)} is used by {usage_words(used)} and would have "
                    "nothing left to play. Add another wallpaper to it first, or use a "
                    "different playlist there"
                )
            return lambda: (store.remove_entries(pid, chosen), removed)

        def saved(
            result: tuple[playlists.Playlist, tuple[tuple[int, playlists.Entry], ...]],
        ) -> bool:
            updated, removed = result
            left = tuple(entry.id for entry in updated.entries)
            self._shown.toast(
                f"Removed {what} from {quoted(name)}",
                lambda: self._restore(pid, removed, left),
            )
            return True

        self._queue(
            f"{what} {'was' if len(chosen) == 1 else 'were'} not removed from {quoted(name)}",
            prepare,
            saved,
            settle=self._entry_write(pid),
        )
        self._emit_edits()

    def _restore(
        self,
        pid: str,
        removed: tuple[tuple[int, playlists.Entry], ...],
        left: tuple[str, ...],
    ) -> None:
        """Undo one removal: the same entries back where they were, if nothing changed since."""
        store = self._store
        playlist = store.get(pid)
        name = playlist.name if playlist is not None else "the playlist"

        def prepare() -> Callable[[], playlists.Playlist]:
            current = self._require(pid, "Undo")
            if tuple(entry.id for entry in current.entries) != left:
                raise RefusedError(
                    f"{quoted(current.name)} has changed since, so the removal can\u2019t be undone"
                )
            return lambda: store.insert_entries(pid, removed)

        self._queue(
            f"The removal from {quoted(name)} was not undone",
            prepare,
            lambda _restored: True,
            settle=self._entry_write(pid),
        )
        self._emit_edits()

    def move_entry(self, pid: str, entry_id: str, step: int) -> None:
        playlist = self._editable(pid)
        if playlist is None or step == 0:
            return
        store = self._store

        def prepare() -> Callable[[], tuple[playlists.Playlist, playlists.Playlist]]:
            before = self._require(pid, "the move")
            return lambda: (before, store.move_entry_relative(pid, entry_id, step))

        def saved(result: tuple[playlists.Playlist, playlists.Playlist]) -> bool:
            before, moved = result
            return moved != before  # a move past either end changes nothing

        self._queue(
            f"The move in {quoted(playlist.name)} was not saved",
            prepare,
            saved,
            settle=self._entry_write(pid),
        )
        self._emit_edits()

    # -- how it plays ---------------------------------------------------------------------
    def set_playlist_interval(self, pid: str, seconds: int | None) -> None:
        playlist = self._editable(pid)
        if playlist is None:
            return
        shown = self._intervals[pid][0] if pid in self._intervals else playlist.cycle_interval
        if seconds == shown:
            return
        store = self._store
        queue_intent(self._intervals, pid, seconds)
        self._emit_edits()

        def prepare() -> Callable[[], tuple[playlists.Playlist, playlists.Playlist]]:
            before = self._require(pid, "the interval")
            return lambda: (before, store.set_rotation(pid, cycle_interval=seconds))

        def saved(result: tuple[playlists.Playlist, playlists.Playlist]) -> bool:
            before, updated = result
            return updated != before

        self._queue(
            f"The interval of {quoted(playlist.name)} was not saved",
            prepare,
            saved,
            settle=lambda: settle_intent(self._intervals, pid),
        )

    def set_playlist_shuffle(self, pid: str, shuffle: bool | None) -> None:
        playlist = self._editable(pid)
        if playlist is None:
            return
        shown = self._shuffles[pid][0] if pid in self._shuffles else playlist.shuffle
        if shuffle == shown:
            return
        store = self._store
        queue_intent(self._shuffles, pid, shuffle)
        self._emit_edits()

        def prepare() -> Callable[[], tuple[playlists.Playlist, playlists.Playlist]]:
            before = self._require(pid, "the shuffle")
            return lambda: (before, store.set_rotation(pid, shuffle=shuffle))

        def saved(result: tuple[playlists.Playlist, playlists.Playlist]) -> bool:
            before, updated = result
            return updated != before

        self._queue(
            f"The shuffle of {quoted(playlist.name)} was not saved",
            prepare,
            saved,
            settle=lambda: settle_intent(self._shuffles, pid),
        )

    def make_default(self, pid: str) -> None:
        detail = self.detail(pid)
        if detail is None or detail.is_default:
            return
        if detail.default_blocked:
            self._backend.window_report(
                f"{quoted(detail.name)} can\u2019t be the default: {detail.default_blocked}"
            )
            return
        queue_intent(self._defaults, _DEFAULT, pid)
        self._default_asked[pid] = self._default_asked.get(pid, 0) + 1
        self._emit_edits()
        settled = False

        def settle() -> None:
            nonlocal settled
            if not settled:
                settled = True
                settle_intent(self._defaults, _DEFAULT)
                left = self._default_asked.get(pid, 0) - 1
                if left > 0:
                    self._default_asked[pid] = left
                else:
                    self._default_asked.pop(pid, None)
                self._emit_edits()

        def failed(error: str) -> None:
            settle()
            self._backend.window_report(
                f"{quoted(detail.name)} was not made the default; nothing changed: {error}"
            )

        accepted = self._backend.update_settings_async(
            active_playlist=pid, on_success=lambda _settings: settle(), on_error=failed
        )
        if not accepted:
            settle()

    def play(self, pid: str) -> None:
        detail = self.detail(pid)
        if detail is None:
            return
        if detail.play_blocked:
            self._backend.window_report(
                f"{quoted(detail.name)} can\u2019t play: {detail.play_blocked}"
            )
            return
        self._backend.activate_playlist_async(pid)
