"""Which playlist each screen shows.

The last piece of step 14, and the only part of it that is a *decision* rather
than plumbing: the renderers have always taken a connector, `wallpaper.outputs`
now says what the connectors are, and this says what each of them should show.

Deliberately a map from connector to playlist name rather than to a wallpaper.
A wallpaper pinned to a screen is a screen that never changes again, which is
not what anybody wants from a wallpaper manager; a playlist per screen is
"cityscapes on the big one, something quiet on the laptop" and still rotates.

A connector with no entry falls back to whatever the app is doing generally,
which is what every single-screen setup wants and is also what happens the
moment a monitor is unplugged. Assignments for screens that are not currently
attached are **kept**, not pruned: unplugging a dock at the end of the day
should not silently forget the arrangement, and a stale entry costs one lookup
that misses.

Written through on every change, like the playlists and the favourites, for
the same reason -- a session that ends any way other than the close button
should not lose the arrangement.

Version 2 adds one opt-in per assigned screen: its own playlist beats the
*global* schedule rules (rules aimed at that screen still win). Every save is
written as version 2; version 1 is still read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from wall_in_one import file_io, paths
from wall_in_one.library import state_file

STATE_FILENAME: Final = "displays.json"
BROKEN_SUFFIX: Final = ".broken"
#: The newest version this build understands. A newer version is shown but
#: never compiled or rewritten: every mutation is refused (kind
#: ``newer-version``). Unknown top-level keys in a known version are carried
#: through every save.
FORMAT_VERSION: Final = 2
FORMATS: Final = state_file.FormatVersions(oldest=1, current=FORMAT_VERSION)

#: Version 2: the sorted connectors whose own playlist beats global rules.
BEATS_GLOBAL_RULES_KEY: Final = "beats_global_rules"

#: What this build models. The ``displays`` map itself is connector to
#: playlist, so only the top level can carry fields this build does not know.
DOCUMENT_SHAPE: Final = state_file.Shape(
    known=frozenset({"version", "displays", BEATS_GLOBAL_RULES_KEY})
)

#: A connector name is short. This is a ceiling on damage from a file somebody
#: has been editing, not a limit anybody will meet.
MAX_STATE_BYTES: Final = 64 * 1024
MAX_ENTRIES: Final = 64
MAX_NAME_BYTES: Final = 256

_MutationResult = TypeVar("_MutationResult")


class DisplayError(Exception):
    """An assignment could not be stored.

    Kinds in use: ``local-io``, ``validation``, ``newer-version``, ``full``,
    ``unreadable``.
    """

    def __init__(self, kind: str, message: str) -> None:
        # For a person: no descriptor spelling of a path (`file_io.public_text`).
        super().__init__(file_io.public_text(message))
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def _unreadable_error(fault: str, target: Path) -> DisplayError:
    return DisplayError(
        state_file.UNREADABLE,
        f"{target.name} can't be read ({fault}), so which assignments it holds isn't known. "
        "Nothing was changed.",
    )


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _clean(value: object) -> str:
    """A playlist name, or ``""`` for anything unusable."""
    if not isinstance(value, str):
        return ""
    flattened = " ".join(value.split())
    try:
        encoded = flattened.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if not flattened or len(encoded) > MAX_NAME_BYTES:
        return ""
    return flattened


def _clean_connector(value: object) -> str:
    """One protocol-addressable connector token, with no whitespace."""
    if not isinstance(value, str):
        return ""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if (
        not value
        or any(character.isspace() for character in value)
        or len(encoded) > MAX_NAME_BYTES
        or any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        return ""
    return value


@dataclass(frozen=True, slots=True)
class Arrangement:
    """Every assignment, and which assigned screens beat global rules."""

    assignments: Mapping[str, str] = field(default_factory=dict)
    beats_global_rules: frozenset[str] = frozenset()


def _read(path: Path) -> state_file.Reading[Arrangement]:
    """Stored assignments, plus why the file was passed over."""
    document, fault = state_file.read_object(
        path, maximum_bytes=MAX_STATE_BYTES, description="display assignments"
    )
    if document is None:
        return state_file.Reading(Arrangement(), fault)
    found, fault = _parse(path, document)
    unknown = state_file.capture_unknown(document, DOCUMENT_SHAPE)
    newer = state_file.newer_version_fault(path, document, FORMAT_VERSION)
    if newer is not None:
        return state_file.Reading(found, newer, newer_version=True, unknown=unknown)
    return state_file.Reading(found, fault, unknown=unknown, version=FORMATS.declared(document))


def reads_cleanly(path: Path | None = None) -> bool:
    """Whether the file is there and reads without a fault, in a version this build knows."""
    reading = _read(path if path is not None else state_path())
    return reading.fault is None and reading.version is not None


def record_ids(contents: bytes) -> frozenset[str] | None:
    """The identities of the records in a document of this store, or ``None``.

    ``None`` when ``contents`` is not a document this build reads in full
    (`state_file.decode_object`, the reader the store ends in, or `_parse`
    finds a fault: a repeated key, NaN, a wrong shape), so a copy that fails
    this is never taken away.
    """
    document, problem = state_file.decode_object(
        contents, STATE_FILENAME, maximum_bytes=MAX_STATE_BYTES, description="display assignments"
    )
    if document is None or problem is not None:
        return None
    found, fault = _parse(Path(STATE_FILENAME), document)
    return None if fault is not None else frozenset(found.assignments)


def held_record_ids(path: Path | None = None) -> frozenset[str] | None:
    """The identities of the records the file holds, or ``None`` unless it reads in full."""
    reading = _read(path if path is not None else state_path())
    if reading.fault is not None or reading.version is None:
        return None
    return frozenset(reading.value.assignments)


def _parse(path: Path, document: dict[str, Any]) -> tuple[Arrangement, str | None]:
    faults: list[str] = []
    if FORMATS.declared(document) is None:
        faults.append(
            f"{path.name} has unsupported version {document.get('version')!r}; "
            f"expected {FORMATS.oldest} to {FORMATS.current}"
        )

    found: dict[str, str] = {}
    entries = document.get("displays")
    if not isinstance(entries, dict):
        return Arrangement(), f"{path.name} has no display assignments in it"
    if len(entries) > MAX_ENTRIES:
        faults.append(f"{path.name} has more than {MAX_ENTRIES} display assignments")
    malformed = 0
    for key, value in entries.items():
        if len(found) >= MAX_ENTRIES:
            break
        connector, playlist = _clean_connector(key), _clean(value)
        if connector and playlist:
            found[connector] = playlist
        else:
            malformed += 1
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed display assignments")

    beating: set[str] = set()
    flagged = document.get(BEATS_GLOBAL_RULES_KEY, [])
    if not isinstance(flagged, list):
        faults.append(f"{path.name} has a malformed {BEATS_GLOBAL_RULES_KEY} list")
        flagged = []
    stray = 0
    for value in flagged[:MAX_ENTRIES]:
        connector = _clean_connector(value)
        # The opt-in belongs to an assignment; one without it is damage.
        if connector and connector in found and connector not in beating:
            beating.add(connector)
        else:
            stray += 1
    stray += max(0, len(flagged) - MAX_ENTRIES)
    if stray:
        faults.append(f"{path.name} has {stray} malformed {BEATS_GLOBAL_RULES_KEY} entries")
    return Arrangement(found, frozenset(beating)), state_file.joined_faults(faults)


def _render(
    assignments: Mapping[str, str],
    target: Path,
    *,
    unknown: state_file.Unknown = state_file.NOTHING_UNKNOWN,
    beats_global_rules: Iterable[str] = (),
) -> str:
    """The document ``save`` writes; refused if this build could not read it back."""
    beating = sorted(set(beats_global_rules))
    if any(connector not in assignments for connector in beating):
        raise ValueError("only an assigned display can beat global rules")
    fields: dict[str, Any] = {"version": FORMATS.current, "displays": dict(assignments)}
    if beating:
        fields[BEATS_GLOBAL_RULES_KEY] = beating
    payload = state_file.merge_unknown(fields, unknown, DOCUMENT_SHAPE)
    try:
        return state_file.render_json(payload, target, maximum_bytes=MAX_STATE_BYTES)
    except state_file.DocumentTooLargeError as error:
        raise DisplayError(
            "full", state_file.too_large_refusal(error, "clear some display assignments")
        ) from error


def _write(target: Path, document: str, *, replace_existing: bool = True) -> None:
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise DisplayError(
            "local-io", f"could not prepare {target.parent}: {error.strerror or error}"
        ) from error
    try:
        state_file.write_atomic_text(target, document, replace_existing=replace_existing)
    except (OSError, UnicodeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise DisplayError("local-io", f"could not write {target}: {detail}") from error


def save(
    assignments: Mapping[str, str],
    path: Path | None = None,
    *,
    replace_existing: bool = True,
    unknown: state_file.Unknown = state_file.NOTHING_UNKNOWN,
    beats_global_rules: Iterable[str] = (),
) -> Path:
    """Write the assignments, atomically, carrying ``unknown`` fields back in.

    The document is always written at `FORMATS`'s current version. A document
    larger than ``MAX_STATE_BYTES``, which this build's reader would refuse, is refused
    (``full``) before anything is written.
    """
    target = path if path is not None else state_path()
    _write(
        target,
        _render(
            assignments,
            target,
            unknown=unknown,
            beats_global_rules=beats_global_rules,
        ),
        replace_existing=replace_existing,
    )
    return target


@dataclass(slots=True)
class _Working:
    """The arrangement one locked mutation edits."""

    assignments: dict[str, str]
    beats_global_rules: set[str]

    @classmethod
    def of(cls, arrangement: Arrangement) -> _Working:
        return cls(dict(arrangement.assignments), set(arrangement.beats_global_rules))

    def frozen(self) -> Arrangement:
        return Arrangement(dict(self.assignments), frozenset(self.beats_global_rules))

    def drop(self, connector: str) -> None:
        """Forget one assignment, and its opt-in with it."""
        self.assignments.pop(connector, None)
        self.beats_global_rules.discard(connector)


class Store:
    """Connector to playlist name, and the file it lives in."""

    def __init__(
        self,
        assignments: Mapping[str, str] | None = None,
        path: Path | None = None,
        *,
        beats_global_rules: Iterable[str] = (),
        _loaded: bool = False,
    ) -> None:
        self._assignments: dict[str, str] = dict(assignments or {})
        self._beats_global_rules: frozenset[str] = frozenset(
            connector for connector in beats_global_rules if connector in self._assignments
        )
        self._path = path
        self._fault: str | None = None
        self._fault_kind: str | None = None
        # Direct construction may intentionally seed an absent file. Opening
        # one is a disk snapshot even when the file was absent, so its later
        # mutation must rebase rather than overwrite another process's write.
        self._loaded = _loaded

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        reading = _read(target)
        store = cls(
            reading.value.assignments,
            target,
            beats_global_rules=reading.value.beats_global_rules,
            _loaded=True,
        )
        store._fault = reading.fault
        store._fault_kind = reading.fault_kind
        return store

    def worker_copy(self, *, rebase: bool = False) -> Store:
        """A detached copy; with ``rebase``, what the file holds, read on a worker.

        As `playlists.Store.worker_copy`: a directly constructed Store may
        seed an absent file, so its assignments are kept until a file has
        existed; an opened one treats absence as what is saved.
        """
        target = self._path if self._path is not None else state_path()
        if rebase:
            try:
                target.lstat()
            except FileNotFoundError:
                if self._loaded:
                    return type(self).open(target)
            except OSError:
                return type(self).open(target)
            else:
                return type(self).open(target)
        copied = type(self)(
            self._assignments,
            target,
            beats_global_rules=self._beats_global_rules,
            _loaded=self._loaded,
        )
        copied._fault = self._fault
        copied._fault_kind = self._fault_kind
        return copied

    @property
    def fault(self) -> str | None:
        return self._fault

    @property
    def fault_kind(self) -> str | None:
        """``newer-version`` (read-only here), ``unreadable``, or ``None``."""
        return self._fault_kind

    def __len__(self) -> int:
        return len(self._assignments)

    def all(self) -> tuple[tuple[str, str], ...]:
        """Every assignment, by connector, so a listing is stable between runs."""
        return tuple(sorted(self._assignments.items()))

    def playlist_for(self, connector: str) -> str:
        """The playlist ``connector`` should show, or ``""`` for the default.

        Empty is the ordinary answer, not a failure: it means this screen has
        no opinion and follows whatever the app is doing generally.
        """
        return self._assignments.get(_clean_connector(connector), "")

    def beats_global_rules(self, connector: str) -> bool:
        """Whether ``connector``'s own playlist beats global schedule rules.

        Off unless somebody opted this screen in. Rules aimed at this screen
        still beat its playlist either way, and only an assigned screen can
        opt in.
        """
        return _clean_connector(connector) in self._beats_global_rules

    def set_beats_global_rules(self, connector: str, enabled: bool) -> bool:
        """Opt one assigned screen in or out; ``False`` if nothing changed.

        Turning it on is the first use of the version-2 field: the file moves
        to version 2 on this save (after keeping the version-1 bytes). The
        runtime reads it from ``runtime-overrides.toml``, which a running
        service that predates it ignores until it restarts.
        """
        name = _clean_connector(connector)
        if not name:
            raise DisplayError("validation", "that is not a connector name")
        if type(enabled) is not bool:
            raise DisplayError("validation", "the display precedence must be on or off")

        def opt(working: _Working) -> tuple[bool, bool]:
            if enabled and name not in working.assignments:
                raise DisplayError(
                    "validation",
                    f"{name} has no playlist of its own; assign one before it can beat "
                    "global schedule rules",
                )
            changed = (name in working.beats_global_rules) != enabled
            if enabled:
                working.beats_global_rules.add(name)
            else:
                working.beats_global_rules.discard(name)
            return changed, changed

        # Opting out of an unreadable file would be a no-op said as a success.
        return self._mutate(opt, refuse_unreadable=True)

    def assign(self, connector: str, playlist: str) -> None:
        """Point one screen at one playlist. Its opt-in, if any, is kept."""
        name, wanted = _clean_connector(connector), _clean(playlist)
        if not name:
            raise DisplayError("validation", "that is not a connector name")
        if not wanted:
            raise DisplayError("validation", "that is not a playlist name")

        def assign(working: _Working) -> tuple[None, bool]:
            assignments = working.assignments
            if len(assignments) >= MAX_ENTRIES and name not in assignments:
                raise DisplayError(
                    "validation", f"no more than {MAX_ENTRIES} screens can be assigned"
                )
            changed = assignments.get(name) != wanted
            assignments[name] = wanted
            return None, changed

        self._mutate(assign)

    def unassign(self, connector: str) -> bool:
        """Let a screen follow the default again. ``False`` if it already did.

        Its opt-in goes too: without a playlist of its own there is nothing
        left to beat the global rules with.
        """
        name = _clean_connector(connector)

        def unassign(working: _Working) -> tuple[bool, bool]:
            changed = name in working.assignments
            working.drop(name)
            return changed, changed

        return self._mutate(unassign, refuse_unreadable=True)

    def forget_display(self, connector: str) -> str:
        """Unassign ``connector`` for Forget Display; return the playlist it had, or "".

        Like `unassign`, its opt-in goes too. A file that can't be read is
        refused before anything is written (``unreadable``): whether it holds
        an assignment can't be known, so it can't be said to be gone.
        """
        name = _clean_connector(connector)

        def forget(working: _Working) -> tuple[str, bool]:
            playlist = working.assignments.get(name, "")
            working.drop(name)
            return playlist, bool(playlist)

        return self._mutate(forget, refuse_unreadable=True)

    def durable_problem(self) -> str:
        """Why a change can't be made to the file as it is now, or empty. Never writes.

        For a caller that must not write anything elsewhere first when this
        file would refuse: one from a newer version, or one that can't be read.
        """
        target = self._target()
        reading = _read(target)
        if reading.newer_version:
            return str(
                DisplayError(state_file.NEWER_VERSION, state_file.newer_version_refusal(target))
            )
        if reading.fault is not None:
            return str(_unreadable_error(reading.fault, target))
        return ""

    def _target(self) -> Path:
        return self._path if self._path is not None else state_path()

    def forget_playlist(self, playlist: str) -> int:
        """Drop every assignment naming ``playlist``, and say how many.

        Called when a playlist is deleted. Without it a screen would keep
        pointing at a name that resolves to nothing, which reads as "this
        screen is broken" rather than "that list is gone". Their opt-ins go
        with them.
        """
        wanted = _clean(playlist)

        def forget(working: _Working) -> tuple[int, bool]:
            stale = [key for key, value in working.assignments.items() if value == wanted]
            for key in stale:
                working.drop(key)
            return len(stale), bool(stale)

        return self._mutate(forget, refuse_unreadable=True)

    def forget_playlist_where(self, names: Callable[[str], bool]) -> int:
        """Drop every assignment whose playlist ``names`` says is the deleted one.

        ``names`` is the compiler's own lookup (id, then name in any letter
        case), and it judges the assignments as they are read under the lock:
        one another program saved since this store last read the file goes
        too. Their opt-ins go with them, as in `forget_playlist`.
        """

        def forget(working: _Working) -> tuple[int, bool]:
            stale = [key for key, value in working.assignments.items() if names(value)]
            for key in stale:
                working.drop(key)
            return len(stale), bool(stale)

        return self._mutate(forget, refuse_unreadable=True)

    def retarget_where(self, names: Callable[[str], bool], playlist_id: str) -> int:
        """Point every assignment ``names`` picks at ``playlist_id``, in one save.

        As `retarget_many`, but which assignments is judged on what is read
        under the lock (see `forget_playlist_where`).
        """
        replacement = _clean(playlist_id)
        if not replacement:
            raise DisplayError("validation", "that is not a playlist name")

        def retarget(working: _Working) -> tuple[int, bool]:
            stale = [
                key
                for key, value in working.assignments.items()
                if value != replacement and names(value)
            ]
            for key in stale:
                working.assignments[key] = replacement
            return len(stale), bool(stale)

        return self._mutate(retarget, refuse_unreadable=True)

    def retarget(self, reference: str, playlist_id: str) -> int:
        """Point every assignment naming ``reference`` at ``playlist_id``; say how many.

        For a playlist about to be renamed while an older profile's
        assignment names it by name: its id names the same playlist before
        and after. One save, and none when nothing names it. See `retarget_many`.
        """
        return self.retarget_many((reference,), playlist_id)

    def retarget_many(self, references: Iterable[str], playlist_id: str) -> int:
        """Point every assignment naming any of ``references`` at ``playlist_id``, in one save.

        All of them or none, and an unreadable file refuses (``unreadable``)
        rather than retargeting nothing. Returns how many assignments changed.
        """
        wanted = frozenset(_clean(reference) for reference in references)
        replacement = _clean(playlist_id)
        if not replacement:
            raise DisplayError("validation", "that is not a playlist name")

        def retarget(working: _Working) -> tuple[int, bool]:
            stale = [key for key, value in working.assignments.items() if value in wanted]
            for key in stale:
                working.assignments[key] = replacement
            return len(stale), bool(stale)

        return self._mutate(retarget, refuse_unreadable=True)

    def adopt_worker_retarget(self, retargeted: Mapping[str, str]) -> int:
        """Mirror a detached `retarget` (reference to playlist id) without another write."""
        cleaned = {_clean(reference): _clean(target) for reference, target in retargeted.items()}
        stale = [key for key, value in self._assignments.items() if value in cleaned]
        for key in stale:
            self._assignments[key] = cleaned[self._assignments[key]]
        return len(stale)

    def adopt_worker_forget_playlists(self, playlists: Iterable[str]) -> int:
        """Mirror detached dangling-reference cleanup without another write."""
        removed = frozenset(_clean(playlist) for playlist in playlists)
        stale = [
            connector for connector, playlist in self._assignments.items() if playlist in removed
        ]
        for connector in stale:
            del self._assignments[connector]
        self._beats_global_rules = self._beats_global_rules.difference(stale)
        return len(stale)

    def describe(self, attached: Iterable[str] = ()) -> tuple[str, ...]:
        """One line per assignment, marking any screen that is not plugged in.

        Detached screens are listed rather than hidden: the whole reason they
        are kept is so somebody can see the arrangement they set up for a dock
        that is not on the desk right now.
        """
        present = frozenset(attached)
        lines: list[str] = []
        for connector, playlist in self.all():
            missing = "" if not present or connector in present else "  (not attached)"
            beats = "  (beats global rules)" if connector in self._beats_global_rules else ""
            lines.append(f"{connector}\t{playlist}{beats}{missing}")
        return tuple(lines)

    def _mutate(
        self,
        change: Callable[[_Working], tuple[_MutationResult, bool]],
        *,
        refuse_unreadable: bool = False,
    ) -> _MutationResult:
        """Apply one connector mutation to the latest durable assignments."""
        target = self._path if self._path is not None else state_path()
        try:
            with (
                state_file.mutation_lock(target, description="display assignments"),
                state_file.observe(target) as observed,
            ):
                try:
                    target.lstat()
                except FileNotFoundError:
                    present = False
                except OSError as error:
                    raise DisplayError(
                        "local-io",
                        f"could not inspect {target}: {error.strerror or error}",
                    ) from error
                else:
                    present = True

                reading = _read(target)
                if reading.newer_version:
                    # Decided from this locked read, not from memory. Adopt
                    # the fault (not the value) so this process stops
                    # compiling its older snapshot over the newer file.
                    self._fault = reading.fault
                    self._fault_kind = reading.fault_kind
                    raise DisplayError(
                        state_file.NEWER_VERSION, state_file.newer_version_refusal(target)
                    )
                current, fault = reading.value, reading.fault
                using_durable = present or self._loaded
                working = _Working.of(
                    current
                    if using_durable
                    else Arrangement(self._assignments, self._beats_global_rules)
                )
                if using_durable:
                    # Report the current disk fault even when the requested
                    # semantic change later fails; do not adopt its value
                    # until persistence succeeds or no write is required.
                    self._fault = fault
                    self._fault_kind = reading.fault_kind
                    self._loaded = True
                if fault is not None and (refuse_unreadable or state_file.read_failed(fault)):
                    # Decided from this locked read: a removal, an opt-out or
                    # a retarget must see the assignments, and can't; and a
                    # file that couldn't be read at all is not damaged, so
                    # nothing may move it aside or write over it.
                    raise _unreadable_error(fault, target)
                result, changed = change(working)
                if changed:
                    # Refused if too large to read back, before anything is
                    # moved; `save` then renders and writes it.
                    _render(
                        working.assignments,
                        target,
                        unknown=reading.unknown,
                        beats_global_rules=working.beats_global_rules,
                    )
                    if fault is not None:
                        try:
                            backup = state_file.preserve_faulted(target, observed=observed)
                        except OSError as error:
                            raise DisplayError(
                                "local-io",
                                f"could not preserve unreadable {target}: "
                                f"{error.strerror or error}",
                            ) from error
                        try:
                            with state_file.published_counts():
                                save(
                                    working.assignments,
                                    target,
                                    replace_existing=False,
                                    unknown=reading.unknown,
                                    beats_global_rules=working.beats_global_rules,
                                )
                        except DisplayError as error:
                            # The damaged original is safe at ``backup``: say where,
                            # and that no new file was written, never "nothing changed".
                            raise DisplayError(
                                error.kind, state_file.replacement_not_saved(target, backup, error)
                            ) from error
                    else:
                        with state_file.published_counts():
                            save(
                                working.assignments,
                                target,
                                unknown=reading.unknown,
                                beats_global_rules=working.beats_global_rules,
                            )
                    fault = None

                # Adopt only after persistence, so a failed save leaves the
                # Store on the last state it could truthfully report.
                self._assignments = working.assignments
                self._beats_global_rules = frozenset(working.beats_global_rules)
                self._fault = fault
                self._fault_kind = state_file.fault_kind(fault, newer_version=False)
                self._loaded = using_durable or changed
                return result
        except DisplayError:
            raise
        except OSError as error:
            raise DisplayError(
                "local-io",
                f"could not safely update display assignments at {target}: "
                f"{error.strerror or error}",
            ) from error
