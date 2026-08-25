"""Which playlist is in force right now, according to the calendar.

A pinned default with ordered overrides on top. The default is
`config.Settings.active_playlist` -- what step 12 already added -- and a rule
here replaces it while the rule matches. Rules are evaluated in order and the
**last** match wins, so a rule added later can carve an exception out of an
earlier one without either being rewritten. That is the arrangement the plugin
this replaces settled on, and it is the one that makes "weekends, except the
first weekend of December" expressible by adding a row rather than editing one.

Resolution is pure and the clock is an argument. Nothing here reads the time,
sleeps, or owns a timer: the UI layer already has a main loop for the cycle
timer and is the only place a periodic re-check belongs. It also means a
schedule can be tested at three in the morning in December without waiting.

Times are local and inclusive of the start, exclusive of the end, so two
adjacent rules do not both match at the boundary. A window whose end is before
its start wraps midnight, because "22:00 to 06:00" is a thing people mean and
the alternative is making them write two rules.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypeVar

from wall_in_one import paths
from wall_in_one.library import state_file

#: The file, under `paths.app_state_dir()`.
STATE_FILENAME: Final = "schedules.json"

FORMAT_VERSION: Final = 2
LEGACY_FORMAT_VERSION: Final = 1

#: Ceilings, so a file that grew a zero cannot be read forever.
MAX_RULES: Final = 512
MAX_STATE_BYTES: Final = 4 * 1024 * 1024

BROKEN_SUFFIX: Final = ".broken"

#: Monday is 0, matching `datetime.weekday()`. Named so a caller never has to
#: remember whether this library counts from Sunday.
WEEKDAY_NAMES: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MINUTES_IN_A_DAY: Final = 24 * 60
MAX_CONNECTOR_BYTES: Final = 256

_MutationResult = TypeVar("_MutationResult")


class ScheduleError(Exception):
    """A rule could not be made or stored, with a machine-readable reason.

    Kinds in use: ``local-io``, ``no-such-rule``, ``identity-conflict``,
    ``invalid-time``, ``invalid-day``, ``invalid-month``,
    ``invalid-connector``, ``full``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def new_id() -> str:
    return secrets.token_hex(8)


def clean_connector(raw: str) -> str:
    """A connector target, or the empty global target.

    Connector names are configuration identities rather than display labels:
    preserve their punctuation and internal spaces exactly, trimming only the
    accidental whitespace around a hand-edited value.
    """
    connector = raw
    try:
        encoded = connector.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ScheduleError("invalid-connector", "connector must be valid UTF-8") from error
    if len(encoded) > MAX_CONNECTOR_BYTES:
        raise ScheduleError(
            "invalid-connector",
            f"connector must be at most {MAX_CONNECTOR_BYTES} UTF-8 bytes",
        )
    if any(character.isspace() for character in connector):
        raise ScheduleError("invalid-connector", "connector cannot contain whitespace")
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in connector):
        raise ScheduleError("invalid-connector", "connector cannot contain control characters")
    return connector


def parse_time(raw: str) -> int:
    """`HH:MM` as minutes past midnight, or raise.

    Returned as an int because every comparison downstream is arithmetic on a
    wrapping day, and doing that with `time` objects means special-casing the
    wrap in three places instead of one.
    """
    text = raw.strip()
    hours, separator, minutes = text.partition(":")
    if not separator or not hours.isdigit() or not minutes.isdigit():
        raise ScheduleError("invalid-time", f"{raw!r} is not a time, which looks like 07:30")
    hour, minute = int(hours), int(minutes)
    if hour > 23 or minute > 59:
        raise ScheduleError("invalid-time", f"{raw!r} is not a time of day")
    return hour * 60 + minute


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_weekdays(raw: Iterable[str]) -> frozenset[int]:
    """`mon`, `tue`... to `datetime.weekday()` numbers. Empty means every day."""
    found: set[int] = set()
    for name in raw:
        key = name.strip().casefold()[:3]
        if key not in WEEKDAY_NAMES:
            raise ScheduleError("invalid-day", f"{name!r} is not a day, which looks like mon")
        found.add(WEEKDAY_NAMES.index(key))
    return frozenset(found)


def parse_months(raw: Iterable[str | int]) -> frozenset[int]:
    """Month numbers, 1 to 12. Empty means every month."""
    found: set[int] = set()
    for value in raw:
        try:
            month = int(value)
        except (TypeError, ValueError) as error:
            raise ScheduleError("invalid-month", f"{value!r} is not a month number") from error
        if not 1 <= month <= 12:
            raise ScheduleError("invalid-month", f"{month} is not a month number")
        found.add(month)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class Rule:
    """One override: a playlist, and when it applies.

    Every empty field means "any", so a rule with nothing set matches always
    and is the way to say "this playlist, until I say otherwise".
    """

    id: str
    playlist: str
    #: Empty targets the shared schedule. A named connector participates only
    #: when independent display control is enabled; global and matching named
    #: rules remain in one ordered list so the established last-match-wins
    #: rule stays literal.
    connector: str = ""
    months: frozenset[int] = frozenset()
    weekdays: frozenset[int] = frozenset()
    #: Minutes past midnight. ``None`` for either means the whole day.
    start: int | None = None
    end: int | None = None
    enabled: bool = True

    def matches(self, at: datetime, connector: str = "") -> bool:
        if self.connector and self.connector != connector:
            return False
        if not self.enabled:
            return False
        calendar_at = self._calendar_date(at)
        if self.months and calendar_at.month not in self.months:
            return False
        if self.weekdays and calendar_at.weekday() not in self.weekdays:
            return False
        return self._within(at.hour * 60 + at.minute)

    def _calendar_date(self, at: datetime) -> datetime:
        """Date which owns this occurrence of a possibly wrapped window.

        The after-midnight tail of ``Mon 22:00-06:00`` is still Monday's
        scheduled window.  The Luau predecessor used the previous weekday and
        month there; retaining that reading also makes a Dec 31 window reach
        Jan 1 instead of being cut off by its calendar filters.
        """
        if self.start is None or self.end is None or self.start <= self.end:
            return at
        minute = at.hour * 60 + at.minute
        return at - timedelta(days=1) if minute < self.end else at

    def _within(self, minutes: int) -> bool:
        """Inclusive of the start, exclusive of the end, wrapping midnight.

        Exclusive at the end so two adjacent windows do not both match on the
        boundary minute; wrapping because "22:00 to 06:00" is one window to a
        person and making them write two rules would be the library arguing
        with the calendar.
        """
        if self.start is None or self.end is None:
            return True
        if self.start == self.end:
            # A zero-length window is almost certainly a mistake, and reading
            # it as "always" is the interpretation that cannot silently hide a
            # playlist somebody scheduled.
            return True
        if self.start < self.end:
            return self.start <= minutes < self.end
        return minutes >= self.start or minutes < self.end

    def describe(self) -> str:
        """The rule in the words it was written in, for a listing."""
        parts: list[str] = []
        if self.months:
            parts.append("months " + ",".join(str(month) for month in sorted(self.months)))
        if self.weekdays:
            parts.append(",".join(WEEKDAY_NAMES[day] for day in sorted(self.weekdays)))
        if self.start is not None and self.end is not None:
            parts.append(f"{format_time(self.start)}-{format_time(self.end)}")
        return " ".join(parts) if parts else "always"

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "playlist": self.playlist}
        if self.connector:
            payload["connector"] = self.connector
        if self.months:
            payload["months"] = sorted(self.months)
        if self.weekdays:
            payload["weekdays"] = [WEEKDAY_NAMES[day] for day in sorted(self.weekdays)]
        if self.start is not None and self.end is not None:
            payload["start"] = format_time(self.start)
            payload["end"] = format_time(self.end)
        if not self.enabled:
            payload["enabled"] = False
        return payload


def _rule(raw: object) -> Rule | None:
    """One stored rule, or ``None``. One bad rule costs only itself."""
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    playlist = raw.get("playlist")
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(playlist, str) or not playlist.strip():
        return None
    try:
        identifier.encode("utf-8")
        playlist.encode("utf-8")
    except UnicodeEncodeError:
        return None
    connector_value = raw.get("connector", "")
    if not isinstance(connector_value, str):
        return None
    weekdays_value = raw.get("weekdays")
    if isinstance(weekdays_value, list) and any(not isinstance(day, str) for day in weekdays_value):
        return None
    try:
        connector = clean_connector(connector_value)
        if "connector" in raw and not connector:
            return None
        months = parse_months(raw["months"]) if isinstance(raw.get("months"), list) else frozenset()
        weekdays = (
            parse_weekdays(weekdays_value) if isinstance(weekdays_value, list) else frozenset()
        )
        start = parse_time(raw["start"]) if isinstance(raw.get("start"), str) else None
        end = parse_time(raw["end"]) if isinstance(raw.get("end"), str) else None
    except ScheduleError:
        return None
    if (start is None) != (end is None):
        # Half a window is not a window, and guessing the other half would be
        # inventing a schedule the user did not write.
        start = end = None
    enabled = raw.get("enabled")
    return Rule(
        id=identifier.strip(),
        playlist=playlist.strip(),
        connector=connector,
        months=months,
        weekdays=weekdays,
        start=start,
        end=end,
        enabled=enabled if isinstance(enabled, bool) else True,
    )


def resolve_rule(rules: Sequence[Rule], at: datetime, connector: str = "") -> Rule | None:
    """The final applicable rule for one connector, or none.

    A blank connector means the shared/mirrored schedule and intentionally
    excludes connector-targeted rules. A named connector considers both global
    and exactly matching rules in their authored order.
    """
    chosen = None
    for rule in rules:
        if rule.matches(at, connector):
            chosen = rule
    return chosen


def resolve(rules: Sequence[Rule], at: datetime, connector: str = "") -> str:
    """The playlist the rules ask for at ``at``, or ``""`` for the default.

    The last match wins, so a rule added later carves an exception out of an
    earlier one without either being rewritten.
    """
    chosen = resolve_rule(rules, at, clean_connector(connector))
    return chosen.playlist if chosen is not None else ""


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _read(path: Path) -> tuple[tuple[Rule, ...], str | None]:
    payload, fault = state_file.read_object(
        path, maximum_bytes=MAX_STATE_BYTES, description="schedule"
    )
    if payload is None:
        return (), fault
    version = payload.get("version")
    faults: list[str] = []
    if version is not None and not (
        type(version) is int and version in (LEGACY_FORMAT_VERSION, FORMAT_VERSION)
    ):
        faults.append(
            f"{path.name} has unsupported version {version!r}; expected "
            f"{LEGACY_FORMAT_VERSION} or {FORMAT_VERSION}"
        )
    stored = payload.get("rules")
    if not isinstance(stored, list):
        return (), f"{path.name} has no rules in it"

    if len(stored) > MAX_RULES:
        faults.append(f"{path.name} has more than {MAX_RULES} schedule rules")

    found: list[Rule] = []
    malformed = 0
    duplicate = 0
    identifiers: set[str] = set()
    for raw in stored[:MAX_RULES]:
        rule = _rule(raw)
        if rule is None:
            malformed += 1
            continue
        assert isinstance(raw, dict)
        for key, expected in (("months", list), ("weekdays", list), ("start", str), ("end", str)):
            if key in raw and not isinstance(raw[key], expected):
                malformed += 1
        if "connector" in raw and not isinstance(raw["connector"], str):
            malformed += 1
        if "enabled" in raw and not isinstance(raw["enabled"], bool):
            malformed += 1
        if ("start" in raw) != ("end" in raw):
            malformed += 1
        if rule.id in identifiers:
            duplicate += 1
            continue
        identifiers.add(rule.id)
        found.append(rule)
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed schedule record fields")
    if duplicate:
        faults.append(f"{path.name} has {duplicate} duplicate schedule rule ids")
    return tuple(found), state_file.joined_faults(faults)


def load(path: Path | None = None) -> tuple[Rule, ...]:
    """The stored rules, in order. Never raises."""
    rules, _fault = _read(path if path is not None else state_path())
    return rules


def save(
    rules: Sequence[Rule],
    path: Path | None = None,
    *,
    replace_existing: bool = True,
) -> Path:
    target = path if path is not None else state_path()
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise ScheduleError(
            "local-io", f"could not create {target.parent}: {error.strerror or error}"
        ) from error

    payload = {"version": FORMAT_VERSION, "rules": [rule.to_json() for rule in rules]}
    try:
        state_file.write_atomic_text(
            target,
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            replace_existing=replace_existing,
        )
    except (OSError, UnicodeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise ScheduleError("local-io", f"could not write {target}: {detail}") from error
    return target


class Store:
    """The schedule as the running app holds it: an ordered list and a file."""

    def __init__(
        self,
        rules: Sequence[Rule] = (),
        path: Path | None = None,
        *,
        _loaded: bool = False,
    ) -> None:
        self._rules: list[Rule] = list(rules)
        self._path = path
        self._fault: str | None = None
        # A directly constructed Store may intentionally seed an absent file.
        # Store.open is different: even an absent file is a durable snapshot,
        # so a later stale mutation must rebase rather than resurrect the seed.
        self._loaded = _loaded

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        rules, fault = _read(target)
        store = cls(rules, target, _loaded=True)
        store._fault = fault
        return store

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(self._rules)

    @property
    def fault(self) -> str | None:
        return self._fault

    def __len__(self) -> int:
        return len(self._rules)

    def resolve(self, at: datetime, connector: str = "") -> str:
        return resolve(self._rules, at, connector)

    def add(
        self,
        playlist: str,
        *,
        months: Iterable[str | int] = (),
        weekdays: Iterable[str] = (),
        start: str = "",
        end: str = "",
        connector: str = "",
        rule_id: str | None = None,
    ) -> Rule:
        """Append a rule. Later rules win, so appending is how you override."""
        if bool(start) != bool(end):
            raise ScheduleError("invalid-time", "a window needs both a start and an end")
        identifier = rule_id or new_id()
        try:
            identifier.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ScheduleError("identity-conflict", "a schedule id must be valid UTF-8") from error
        try:
            playlist.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ScheduleError(
                "no-such-rule", "a playlist reference must be valid UTF-8"
            ) from error
        rule = Rule(
            id=identifier,
            playlist=playlist.strip(),
            connector=clean_connector(connector),
            months=parse_months(months),
            weekdays=parse_weekdays(weekdays),
            start=parse_time(start) if start else None,
            end=parse_time(end) if end else None,
        )
        if not rule.playlist:
            raise ScheduleError("no-such-rule", "a rule needs a playlist")

        def append(rules: list[Rule]) -> tuple[Rule, bool]:
            if len(rules) >= MAX_RULES:
                raise ScheduleError("full", f"there are already {MAX_RULES} rules")
            if any(current.id == rule.id for current in rules):
                raise ScheduleError(
                    "identity-conflict",
                    f"there is already a schedule rule named {rule.id}",
                )
            rules.append(rule)
            return rule, True

        return self._mutate(append)

    def remove(self, rule_id: str) -> bool:
        def remove(rules: list[Rule]) -> tuple[bool, bool]:
            kept = [rule for rule in rules if rule.id != rule_id]
            changed = len(kept) != len(rules)
            if changed:
                rules[:] = kept
            return changed, changed

        return self._mutate(remove)

    def set_enabled(self, rule_id: str, enabled: bool) -> Rule:
        def set_enabled(rules: list[Rule]) -> tuple[Rule, bool]:
            for index, rule in enumerate(rules):
                if rule.id == rule_id:
                    updated = replace(rule, enabled=enabled)
                    rules[index] = updated
                    return updated, updated != rule
            raise ScheduleError("no-such-rule", f"no rule {rule_id}")

        return self._mutate(set_enabled)

    def update(
        self,
        rule_id: str,
        playlist: str,
        *,
        months: Iterable[str | int] = (),
        weekdays: Iterable[str] = (),
        start: str = "",
        end: str = "",
        connector: str = "",
    ) -> Rule:
        """Edit a rule in place without changing its id or priority."""
        if bool(start) != bool(end):
            raise ScheduleError("invalid-time", "a window needs both a start and an end")
        if not playlist.strip():
            raise ScheduleError("no-such-rule", "a rule needs a playlist")
        try:
            playlist.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ScheduleError(
                "no-such-rule", "a playlist reference must be valid UTF-8"
            ) from error
        chosen_playlist = playlist.strip()
        chosen_connector = clean_connector(connector)
        chosen_months = parse_months(months)
        chosen_weekdays = parse_weekdays(weekdays)
        chosen_start = parse_time(start) if start else None
        chosen_end = parse_time(end) if end else None

        def update(rules: list[Rule]) -> tuple[Rule, bool]:
            for index, rule in enumerate(rules):
                if rule.id != rule_id:
                    continue
                updated = Rule(
                    id=rule.id,
                    playlist=chosen_playlist,
                    connector=chosen_connector,
                    months=chosen_months,
                    weekdays=chosen_weekdays,
                    start=chosen_start,
                    end=chosen_end,
                    enabled=rule.enabled,
                )
                rules[index] = updated
                return updated, updated != rule
            raise ScheduleError("no-such-rule", f"no rule {rule_id}")

        return self._mutate(update)

    def move(self, rule_id: str, position: int) -> Rule:
        """Move one stable rule to ``position``; later rows keep priority."""

        def move(rules: list[Rule]) -> tuple[Rule, bool]:
            for index, rule in enumerate(rules):
                if rule.id != rule_id:
                    continue
                moving = rules.pop(index)
                target = max(0, min(len(rules), position))
                rules.insert(target, moving)
                return moving, target != index
            raise ScheduleError("no-such-rule", f"no rule {rule_id}")

        return self._mutate(move)

    def move_relative(self, rule_id: str, step: int) -> Rule:
        """Move from the rule's latest durable position by ``step``."""

        def move(rules: list[Rule]) -> tuple[Rule, bool]:
            for index, rule in enumerate(rules):
                if rule.id != rule_id:
                    continue
                target = max(0, min(len(rules) - 1, index + step))
                moving = rules.pop(index)
                rules.insert(target, moving)
                return moving, target != index
            raise ScheduleError("no-such-rule", f"no rule {rule_id}")

        return self._mutate(move)

    def forget_playlist(self, playlist: str) -> bool:
        """Drop every rule naming a playlist that has just been deleted.

        A rule pointing at nothing resolves to a playlist that is not there,
        which `playlists.rotation` then falls back from -- so it would look
        like the schedule silently not working rather than like a rule that
        should have gone.
        """

        def forget(rules: list[Rule]) -> tuple[bool, bool]:
            kept = [rule for rule in rules if rule.playlist != playlist]
            changed = len(kept) != len(rules)
            if changed:
                rules[:] = kept
            return changed, changed

        return self._mutate(forget)

    def adopt_worker_forget_playlists(self, playlists: Iterable[str]) -> int:
        """Mirror detached dangling-reference cleanup without another write."""
        removed = frozenset(playlists)
        if not removed:
            return 0
        kept = [rule for rule in self._rules if rule.playlist not in removed]
        changed = len(self._rules) - len(kept)
        if changed:
            self._rules[:] = kept
        return changed

    def _mutate(
        self,
        change: Callable[[list[Rule]], tuple[_MutationResult, bool]],
    ) -> _MutationResult:
        """Apply one semantic mutation to the latest durable rule order.

        Atomic replacement prevents torn JSON, but it cannot stop a stale GUI
        or helper process from replacing another process's valid edit. Every
        Store operation therefore recomputes its change after acquiring the
        shared state-file lock.
        """
        target = self._path if self._path is not None else state_path()
        try:
            with (
                state_file.mutation_lock(target, description="schedules"),
                state_file.observe(target) as observed,
            ):
                try:
                    target.lstat()
                except FileNotFoundError:
                    present = False
                except OSError as error:
                    raise ScheduleError(
                        "local-io",
                        f"could not inspect {target}: {error.strerror or error}",
                    ) from error
                else:
                    present = True

                current, fault = _read(target)
                using_durable = present or self._loaded
                rules = self._reuse_unchanged_rules(current) if using_durable else list(self._rules)
                if using_durable:
                    # Fault/loaded provenance belongs to the disk snapshot we
                    # just observed even if validation or persistence below
                    # fails. The authored value itself is adopted only after
                    # a successful or no-op transaction.
                    self._fault = fault
                    self._loaded = True
                result, changed = change(rules)
                if changed:
                    if fault is not None:
                        try:
                            state_file.preserve_faulted(target, observed=observed)
                        except OSError as error:
                            raise ScheduleError(
                                "local-io",
                                f"could not preserve unreadable {target}: "
                                f"{error.strerror or error}",
                            ) from error
                        save(rules, target, replace_existing=False)
                    else:
                        save(rules, target)
                    fault = None

                # File first, then memory: failed persistence cannot make the
                # live schedule claim a mutation which was never durable.
                self._rules = rules
                self._fault = fault
                self._loaded = using_durable or changed
                return result
        except ScheduleError:
            raise
        except OSError as error:
            raise ScheduleError(
                "local-io",
                f"could not safely update schedules at {target}: {error.strerror or error}",
            ) from error

    def _reuse_unchanged_rules(self, current: Sequence[Rule]) -> list[Rule]:
        """Rebase values without needlessly invalidating live Rule objects."""
        available: dict[Rule, list[Rule]] = {}
        for rule in self._rules:
            available.setdefault(rule, []).append(rule)
        reused: list[Rule] = []
        for rule in current:
            matches = available.get(rule)
            reused.append(matches.pop() if matches else rule)
        return reused


def describe(
    rules: Sequence[Rule],
    active: str,
    at: datetime,
    names: Mapping[str, str] | None = None,
    connector: str = "",
) -> str:
    """The schedule as rows, marking which rule is in force right now.

    ``names`` maps playlist ids to what they are called. Rules store the id,
    because a rename must not break a schedule -- but a listing full of
    sixteen-character hex is unreadable, and this is output for a person.
    """
    winner = resolve(rules, at, connector)
    default = (names or {}).get(active, active) if active else "(All media)"
    lines = [
        f"# schedule: {len(rules)} rules, default {default}",
        "# fields: rule, playlist, when, enabled, in-force",
    ]
    # The last match wins, so only the final matching rule is in force.
    last_match = ""
    for rule in rules:
        if rule.matches(at, connector):
            last_match = rule.id
    known = names or {}
    for rule in rules:
        in_force = "yes" if rule.id == last_match and winner else "no"
        called = known.get(rule.playlist, rule.playlist)
        lines.append(
            f"{rule.id}\t{called}\t{rule.describe()}\t{'yes' if rule.enabled else 'no'}\t{in_force}"
        )
    return "\n".join(lines)


def effective(rules: Sequence[Rule], default: str, at: datetime, connector: str = "") -> str:
    """The playlist in force: a matching rule, else the pinned default."""
    return resolve(rules, at, connector) or default
