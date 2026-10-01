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
*global* schedule rules (rules aimed at that screen still win). The file moves
to version 2 only when somebody turns that on, after the version-1 bytes are
kept as ``displays.json.v1-backup``; until then it stays version 1.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from wall_in_one import paths
from wall_in_one.library import state_file

STATE_FILENAME: Final = "displays.json"
BROKEN_SUFFIX: Final = ".broken"
#: The newest version this build understands. A newer version is shown but
#: never compiled or rewritten: every mutation is refused (kind
#: ``newer-version``). Unknown top-level keys in a known version are carried
#: through every save.
FORMAT_VERSION: Final = 2
PRECEDENCE_VERSION: Final = 2
FORMATS: Final = state_file.FormatVersions(oldest=1, floor=1, current=FORMAT_VERSION)

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

    Kinds in use: ``local-io``, ``validation``, ``newer-version``,
    ``no-backup``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


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


def required_version(beats_global_rules: Iterable[str]) -> int:
    """The oldest format that can hold the arrangement (lazy bump on use)."""
    return PRECEDENCE_VERSION if any(True for _ in beats_global_rules) else FORMATS.floor


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


def save(
    assignments: Mapping[str, str],
    path: Path | None = None,
    *,
    replace_existing: bool = True,
    unknown: state_file.Unknown = state_file.NOTHING_UNKNOWN,
    beats_global_rules: Iterable[str] = (),
    version: int | None = None,
) -> Path:
    """Write the assignments, atomically, carrying ``unknown`` fields back in.

    ``version`` defaults to the oldest format that holds the arrangement (see
    `required_version`), so a file without opt-ins is written as version 1.
    Keeping a file's newer version, and the backup before a bump, belong to
    `Store`, which knows what is on disk.
    """
    target = path if path is not None else state_path()
    beating = sorted(set(beats_global_rules))
    if any(connector not in assignments for connector in beating):
        raise ValueError("only an assigned display can beat global rules")
    wanted = required_version(beating)
    written = version if version is not None else FORMATS.to_write(None, wanted)
    if wanted > written or not FORMATS.floor <= written <= FORMATS.current:
        raise ValueError(f"displays cannot be saved as version {written}; they need {wanted}")
    fields: dict[str, Any] = {"version": written, "displays": dict(assignments)}
    if beating:
        fields[BEATS_GLOBAL_RULES_KEY] = beating
    payload = state_file.merge_unknown(fields, unknown, DOCUMENT_SHAPE)
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise DisplayError(
            "local-io", f"could not prepare {target.parent}: {error.strerror or error}"
        ) from error
    try:
        state_file.write_atomic_text(
            target,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            replace_existing=replace_existing,
        )
    except (OSError, UnicodeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise DisplayError("local-io", f"could not write {target}: {detail}") from error
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

        return self._mutate(opt)

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

        return self._mutate(unassign)

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

        return self._mutate(forget)

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
                result, changed = change(working)
                if changed:
                    version = FORMATS.to_write(
                        reading.version, required_version(working.beats_global_rules)
                    )
                    if reading.version is not None and FORMATS.is_bump(reading.version, version):
                        _back_up_before_bump(target, observed, reading.version, version)
                    if fault is not None:
                        try:
                            state_file.preserve_faulted(target, observed=observed)
                        except OSError as error:
                            raise DisplayError(
                                "local-io",
                                f"could not preserve unreadable {target}: "
                                f"{error.strerror or error}",
                            ) from error
                        save(
                            working.assignments,
                            target,
                            replace_existing=False,
                            unknown=reading.unknown,
                            beats_global_rules=working.beats_global_rules,
                            version=version,
                        )
                    else:
                        save(
                            working.assignments,
                            target,
                            unknown=reading.unknown,
                            beats_global_rules=working.beats_global_rules,
                            version=version,
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


def _back_up_before_bump(
    target: Path,
    observed: state_file.StateFileObservation,
    replaced: int,
    version: int,
) -> None:
    """Guard 2, or refuse: never bump a file without its old bytes kept."""
    try:
        state_file.backup_before_bump(target, observed=observed, replaced=replaced)
    except OSError as error:
        backup = state_file.version_backup_path(target, replaced)
        raise DisplayError(
            "no-backup",
            f"could not keep a copy of {target.name} (version {replaced}) as {backup.name} "
            f"before saving it as version {version}: {error.strerror or error}. "
            "Nothing was changed.",
        ) from error
