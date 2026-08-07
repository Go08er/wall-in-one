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

import contextlib
import json
import os
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from wall_in_one import paths

#: The file, under `paths.app_state_dir()`.
STATE_FILENAME: Final = "schedules.json"

FORMAT_VERSION: Final = 1

#: Ceilings, so a file that grew a zero cannot be read forever.
MAX_RULES: Final = 512
MAX_STATE_BYTES: Final = 4 * 1024 * 1024

BROKEN_SUFFIX: Final = ".broken"

#: Monday is 0, matching `datetime.weekday()`. Named so a caller never has to
#: remember whether this library counts from Sunday.
WEEKDAY_NAMES: Final[tuple[str, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

MINUTES_IN_A_DAY: Final = 24 * 60


class ScheduleError(Exception):
    """A rule could not be made or stored, with a machine-readable reason.

    Kinds in use: ``local-io``, ``no-such-rule``, ``invalid-time``,
    ``invalid-day``, ``invalid-month``, ``full``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def new_id() -> str:
    return secrets.token_hex(8)


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
    months: frozenset[int] = frozenset()
    weekdays: frozenset[int] = frozenset()
    #: Minutes past midnight. ``None`` for either means the whole day.
    start: int | None = None
    end: int | None = None
    enabled: bool = True

    def matches(self, at: datetime) -> bool:
        if not self.enabled:
            return False
        if self.months and at.month not in self.months:
            return False
        if self.weekdays and at.weekday() not in self.weekdays:
            return False
        return self._within(at.hour * 60 + at.minute)

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
        months = parse_months(raw["months"]) if isinstance(raw.get("months"), list) else frozenset()
        weekdays = (
            parse_weekdays(raw["weekdays"])
            if isinstance(raw.get("weekdays"), list)
            else frozenset()
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
        months=months,
        weekdays=weekdays,
        start=start,
        end=end,
        enabled=enabled if isinstance(enabled, bool) else True,
    )


def resolve(rules: Sequence[Rule], at: datetime) -> str:
    """The playlist the rules ask for at ``at``, or ``""`` for the default.

    The last match wins, so a rule added later carves an exception out of an
    earlier one without either being rewritten.
    """
    chosen = ""
    for rule in rules:
        if rule.matches(at):
            chosen = rule.playlist
    return chosen


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _read(path: Path) -> tuple[tuple[Rule, ...], str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return (), None
        if path.stat().st_size > MAX_STATE_BYTES:
            return (), f"{path.name} is too large to be a schedule"
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return (), f"could not read {path.name}: {error.strerror or error}"

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return (), f"{path.name} is not readable, so no schedule was loaded"
    if not isinstance(payload, dict):
        return (), f"{path.name} is not a schedule file"
    stored = payload.get("rules")
    if not isinstance(stored, list):
        return (), f"{path.name} has no rules in it"

    found: list[Rule] = []
    for raw in stored[:MAX_RULES]:
        rule = _rule(raw)
        if rule is not None:
            found.append(rule)
    return tuple(found), None


def load(path: Path | None = None) -> tuple[Rule, ...]:
    """The stored rules, in order. Never raises."""
    rules, _fault = _read(path if path is not None else state_path())
    return rules


def save(rules: Sequence[Rule], path: Path | None = None) -> Path:
    target = path if path is not None else state_path()
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise ScheduleError(
            "local-io", f"could not create {target.parent}: {error.strerror or error}"
        ) from error

    payload = {"version": FORMAT_VERSION, "rules": [rule.to_json() for rule in rules]}
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ScheduleError(
            "local-io", f"could not write {target}: {error.strerror or error}"
        ) from error
    return target


class Store:
    """The schedule as the running app holds it: an ordered list and a file."""

    def __init__(self, rules: Sequence[Rule] = (), path: Path | None = None) -> None:
        self._rules: list[Rule] = list(rules)
        self._path = path
        self._fault: str | None = None

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        rules, fault = _read(target)
        store = cls(rules, target)
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

    def resolve(self, at: datetime) -> str:
        return resolve(self._rules, at)

    def add(
        self,
        playlist: str,
        *,
        months: Iterable[str | int] = (),
        weekdays: Iterable[str] = (),
        start: str = "",
        end: str = "",
        rule_id: str | None = None,
    ) -> Rule:
        """Append a rule. Later rules win, so appending is how you override."""
        if len(self._rules) >= MAX_RULES:
            raise ScheduleError("full", f"there are already {MAX_RULES} rules")
        if bool(start) != bool(end):
            raise ScheduleError("invalid-time", "a window needs both a start and an end")
        rule = Rule(
            id=rule_id or new_id(),
            playlist=playlist.strip(),
            months=parse_months(months),
            weekdays=parse_weekdays(weekdays),
            start=parse_time(start) if start else None,
            end=parse_time(end) if end else None,
        )
        if not rule.playlist:
            raise ScheduleError("no-such-rule", "a rule needs a playlist")
        self._rules.append(rule)
        self._write()
        return rule

    def remove(self, rule_id: str) -> bool:
        kept = [rule for rule in self._rules if rule.id != rule_id]
        if len(kept) == len(self._rules):
            return False
        self._rules = kept
        self._write()
        return True

    def set_enabled(self, rule_id: str, enabled: bool) -> Rule:
        for index, rule in enumerate(self._rules):
            if rule.id == rule_id:
                updated = replace(rule, enabled=enabled)
                self._rules[index] = updated
                self._write()
                return updated
        raise ScheduleError("no-such-rule", f"no rule {rule_id}")

    def forget_playlist(self, playlist: str) -> bool:
        """Drop every rule naming a playlist that has just been deleted.

        A rule pointing at nothing resolves to a playlist that is not there,
        which `playlists.rotation` then falls back from -- so it would look
        like the schedule silently not working rather than like a rule that
        should have gone.
        """
        kept = [rule for rule in self._rules if rule.playlist != playlist]
        if len(kept) == len(self._rules):
            return False
        self._rules = kept
        self._write()
        return True

    def _write(self) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            broken = target.with_name(target.name + BROKEN_SUFFIX)
            with contextlib.suppress(OSError):
                os.replace(target, broken)
            self._fault = None
        save(self._rules, target)


def describe(
    rules: Sequence[Rule],
    active: str,
    at: datetime,
    names: Mapping[str, str] | None = None,
) -> str:
    """The schedule as rows, marking which rule is in force right now.

    ``names`` maps playlist ids to what they are called. Rules store the id,
    because a rename must not break a schedule -- but a listing full of
    sixteen-character hex is unreadable, and this is output for a person.
    """
    winner = resolve(rules, at)
    default = (names or {}).get(active, active) if active else "(the whole library)"
    lines = [
        f"# schedule: {len(rules)} rules, default {default}",
        "# fields: rule, playlist, when, enabled, in-force",
    ]
    # The last match wins, so only the final matching rule is in force.
    last_match = ""
    for rule in rules:
        if rule.matches(at):
            last_match = rule.id
    known = names or {}
    for rule in rules:
        in_force = "yes" if rule.id == last_match and winner else "no"
        called = known.get(rule.playlist, rule.playlist)
        lines.append(
            f"{rule.id}\t{called}\t{rule.describe()}\t{'yes' if rule.enabled else 'no'}\t{in_force}"
        )
    return "\n".join(lines)


def effective(rules: Sequence[Rule], default: str, at: datetime) -> str:
    """The playlist in force: a matching rule, else the pinned default."""
    return resolve(rules, at) or default
