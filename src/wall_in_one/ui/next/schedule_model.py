"""Schedule words, colors and calendar projection shared by the Schedule page's parts.

Nothing here decides whether a rule applies: every answer comes from the
adapter's `Scheduling.rule_applies` (the app's own matching), asked once per
stretch of time in which no rule can change its mind. And nothing here says
what is in force *now*: that is the runtime's (`Scheduling.routes`). This
module turns those answers into spans, words and colors, and holds the two
mappings the page must not get wrong: the list's top-wins order over the
stored last-wins order, and the time windows a calendar drag may create.

GTK-free.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Final

from wall_in_one.ui.next.schedule_state import MINUTES_IN_A_DAY, RuleView

#: ``rule_applies(rule, at, connector)``.
Applies = Callable[[RuleView, dt.datetime, str], bool]

DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAYS_LONG: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: Index 0 is January: ``MONTHS[month - 1]``.
MONTHS: Final = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_LONG: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS: Final = frozenset(range(5))
WEEKEND: Final = frozenset({5, 6})
EVERY_DAY: Final = frozenset(range(7))
#: The calendar draws at most this many enabled rules; past it, it says so.
MAX_PROJECTED_RULES: Final = 96
#: Calendar drags snap to this many minutes.
SNAP: Final = 15


# -- time ----------------------------------------------------------------------------


def hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def has_window(rule: RuleView) -> bool:
    """A real time window: both ends, and not the same minute."""
    return rule.start is not None and rule.end is not None and rule.start != rule.end


def wraps(rule: RuleView) -> bool:
    """The window runs past midnight (its tail belongs to the day it started)."""
    return (
        has_window(rule)
        and rule.start is not None
        and rule.end is not None
        and rule.start > rule.end
    )


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def window_problem(start: int | None, end: int | None) -> str:
    """Why a rule's time window can't be saved, or empty when it can.

    ``start == end`` is refused rather than stored: the store reads a window
    that starts and ends on the same minute as "always", which is never what
    choosing two times meant.
    """
    if (start is None) != (end is None):
        return "A time window needs both a start and an end"
    if start is None or end is None:
        return ""
    if not (0 <= start < MINUTES_IN_A_DAY and 0 <= end < MINUTES_IN_A_DAY):
        return "A time is between 00:00 and 23:59"
    if start == end:
        return "It starts and ends at the same time; choose All day instead"
    return ""


def window_from_drag(start: int, end: int) -> tuple[int | None, int | None] | None:
    """The window a calendar drag from ``start`` to ``end`` (0..1440) creates, or None.

    A drag with no width is refused. A drag over the whole day is "all day"
    (no window), never ``00:00-00:00``; a drag that ends at midnight wraps to
    an end of 00:00.
    """
    if not (0 <= start < end <= MINUTES_IN_A_DAY):
        return None
    if start == 0 and end == MINUTES_IN_A_DAY:
        return None, None
    return start, end % MINUTES_IN_A_DAY


# -- the list's order ------------------------------------------------------------------


def shown_index(stored_index: int, count: int) -> int:
    """Where stored rule ``stored_index`` of ``count`` sits in the list (top = 0 = wins)."""
    return count - 1 - stored_index


def stored_index(shown: int, count: int) -> int:
    """The inverse of `shown_index`."""
    return count - 1 - shown


def stored_step(shown_from: int, shown_to: int) -> int:
    """The `Scheduling.shift_rule` step for moving a list row from one place to another.

    Up the list (towards the top, which wins) is later in stored order.
    """
    return shown_from - shown_to


# -- words -----------------------------------------------------------------------------


def _runs(values: Iterable[int], size: int, first: int = 0) -> list[list[int]]:
    """Consecutive runs over a cycle of ``size`` values starting at ``first``."""
    chosen = sorted({value - first for value in values})
    if not chosen:
        return []
    start = next((v for v in chosen if (v - 1) % size not in chosen), chosen[0])
    ordered = [v for v in chosen if v >= start] + [v for v in chosen if v < start]
    runs: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value == (runs[-1][-1] + 1) % size:
            runs[-1].append(value)
        else:
            runs.append([value])
    return [[value + first for value in run] for run in runs]


def days_text(days: Iterable[int]) -> str:
    chosen = sorted(set(days))
    if not chosen or len(chosen) == 7:
        return "Every day"
    if len(chosen) == 1:
        return DAYS[chosen[0]]
    parts: list[str] = []
    for run in _runs(chosen, 7):
        if len(run) >= 2 and not (len(run) == 2 and len(chosen) > 3):
            parts.append(f"{DAYS[run[0]]}\u2013{DAYS[run[-1]]}")
        else:
            parts.extend(DAYS[value] for value in run)
    return ", ".join(parts)


def months_text(months: Iterable[int]) -> str:
    """Months 1..12 in words: ``All year``, ``December``, ``Nov-Feb``."""
    chosen = sorted(set(months))
    if not chosen or len(chosen) == 12:
        return "All year"
    if len(chosen) == 1:
        return MONTHS_LONG[chosen[0] - 1]
    parts: list[str] = []
    for run in _runs(chosen, 12, first=1):
        if len(run) >= 3:
            parts.append(f"{MONTHS[run[0] - 1]}\u2013{MONTHS[run[-1] - 1]}")
        else:
            parts.extend(MONTHS[value - 1] for value in run)
    return ", ".join(parts)


def time_text(rule: RuleView, overnight: bool = True) -> str:
    if not has_window(rule) or rule.start is None or rule.end is None:
        return "All day"
    text = f"{hhmm(rule.start)}\u2013{hhmm(rule.end)}"
    if overnight and wraps(rule):
        text += " (overnight)"
    return text


def summary(rule: RuleView, display: bool = True, overnight: bool = True) -> str:
    """``Mon-Fri · 09:00-17:00 · HDMI-A-1``, ``Every day · 18:00-07:00 (overnight)``."""
    parts: list[str] = []
    if has_window(rule):
        parts += [days_text(rule.weekdays), time_text(rule, overnight)]
    elif rule.weekdays:
        parts += [days_text(rule.weekdays), "All day"]
    else:
        parts.append("All day" if rule.months else "Always")
    if rule.months:
        parts.append(months_text(rule.months))
    if display and rule.connector:
        parts.append(rule.connector)
    return " · ".join(parts)


# -- colors: one per playlist, kept distinct --------------------------------------------

#: libadwaita's accent colors: readable in light and dark, white text on top.
PALETTE: Final = (
    "#3584e4",
    "#3a944a",
    "#9141ac",
    "#c88800",
    "#2190a4",
    "#d56199",
    "#ed5b00",
    "#e62d42",
)
FALLBACK_COLOR: Final = "#6f8396"


def playlist_colors(playlists: Sequence[tuple[str, bool]]) -> dict[str, str]:
    """Playlist id -> calendar color, from ``(id, automatic)`` in the sidebar's order."""
    colors: dict[str, str] = {}
    index = 0
    for pid, automatic in playlists:
        if automatic:
            colors[pid] = FALLBACK_COLOR
        else:
            colors[pid] = PALETTE[index % len(PALETTE)]
            index += 1
    return colors


def rgb(hex_color: str) -> tuple[float, float, float]:
    value = hex_color.lstrip("#")
    return (int(value[0:2], 16) / 255, int(value[2:4], 16) / 255, int(value[4:6], 16) / 255)


# -- spans: what plays when, for a week, projected with the app's matching ----------------


@dataclass
class Span:
    day: int  # column 0..6 (Mon..Sun)
    start: int  # minutes from midnight
    end: int  # exclusive, up to 1440
    rule: RuleView | None  # the winner; None = what plays when nothing is scheduled
    losers: list[RuleView] = field(default_factory=list)  # applicable but beaten (low -> high)

    @property
    def top_loser(self) -> RuleView | None:
        return self.losers[-1] if self.losers else None


@dataclass
class DisplayException:
    """A rule for one display, drawn beside the shared calendar."""

    day: int
    start: int
    end: int
    rule: RuleView
    wins: bool
    over: RuleView | None = None


def _at(day: dt.date, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60)


def _breakpoints(rules: Iterable[RuleView]) -> list[int]:
    points = {0, MINUTES_IN_A_DAY}
    for rule in rules:
        if has_window(rule) and rule.start is not None and rule.end is not None:
            points.add(rule.start)
            points.add(rule.end)
    return sorted(points)


def _may_apply_on(rule: RuleView, day: dt.date) -> bool:
    """A cheap prefilter: could ``rule`` apply at some minute of ``day``?

    The tail of yesterday's overnight window counts as yesterday's, so both
    days' month and weekday are allowed.
    """
    if not rule.enabled:
        return False
    days = (day, day - dt.timedelta(days=1)) if wraps(rule) else (day,)
    return any(
        (not rule.months or when.month in rule.months)
        and (not rule.weekdays or when.weekday() in rule.weekdays)
        for when in days
    )


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for span in spans:
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.rule is span.rule
            and last.end == span.start
            and [id(rule) for rule in last.losers] == [id(rule) for rule in span.losers]
        ):
            last.end = span.end
        else:
            merged.append(span)
    return merged


def day_spans(
    rules: Sequence[RuleView], day: dt.date, column: int, connector: str, applies: Applies
) -> list[Span]:
    """Pieces of one day on ``connector`` ("" = shared); inside each, no rule changes its mind."""
    candidates = [rule for rule in rules if _may_apply_on(rule, day)]
    spans = []
    for start, end in pairwise(_breakpoints(candidates)):
        at = _at(day, start)
        matching = [rule for rule in candidates if applies(rule, at, connector)]
        spans.append(Span(column, start, end, matching[-1] if matching else None, matching[:-1]))
    return _merge(spans)


def week_spans(
    rules: Sequence[RuleView], monday: dt.date, connector: str, applies: Applies
) -> list[list[Span]]:
    return [
        day_spans(rules, monday + dt.timedelta(days=column), column, connector, applies)
        for column in range(7)
    ]


def week_exceptions(
    rules: Sequence[RuleView], monday: dt.date, applies: Applies
) -> list[DisplayException]:
    """Each display's own rules, for the shared view (independent displays only)."""
    found: list[DisplayException] = []
    targeted = [rule for rule in rules if rule.connector and rule.enabled]
    for column in range(7):
        day = monday + dt.timedelta(days=column)
        today = [rule for rule in targeted if _may_apply_on(rule, day)]
        if not today:
            continue
        candidates = [rule for rule in rules if _may_apply_on(rule, day)]
        for rule in today:
            pieces: list[DisplayException] = []
            for start, end in pairwise(_breakpoints(candidates)):
                at = _at(day, start)
                if not applies(rule, at, rule.connector):
                    continue
                matching = [other for other in candidates if applies(other, at, rule.connector)]
                wins = matching[-1] is rule
                over = matching[-2] if wins and len(matching) > 1 else None
                last = pieces[-1] if pieces else None
                if last is not None and last.end == start and last.wins == wins:
                    last.end = end
                else:
                    pieces.append(DisplayException(column, start, end, rule, wins, over))
            found += pieces
    return found


def seasonal_off(rules: Iterable[RuleView], monday: dt.date) -> list[RuleView]:
    """Enabled rules limited to months that this week doesn't touch."""
    months = {(monday + dt.timedelta(days=offset)).month for offset in range(7)}
    return [rule for rule in rules if rule.enabled and rule.months and not months & rule.months]


def next_week_for(rule: RuleView, monday: dt.date) -> dt.date:
    """Monday of the first week, from this one on, in which ``rule``'s months apply."""
    probe = monday
    for _ in range(60):
        if any((probe + dt.timedelta(days=offset)).month in rule.months for offset in range(7)):
            return probe
        probe += dt.timedelta(days=7)
    return monday


def never_winning(
    rules: Sequence[RuleView],
    monday: dt.date,
    connectors: Sequence[str],
    applies: Applies,
) -> set[str]:
    """Ids of the rules that apply this week but lose every minute to higher rules.

    Projected once per target (the shared schedule and each display), not
    once per rule.
    """
    targets = ["", *connectors]
    targets += sorted({rule.connector for rule in rules if rule.connector} - set(targets))
    winners: set[int] = set()
    losers: set[int] = set()
    for connector in targets:
        for spans in week_spans(rules, monday, connector, applies):
            for span in spans:
                if span.rule is not None:
                    winners.add(id(span.rule))
                losers.update(id(loser) for loser in span.losers)
    return {
        rule.id for rule in rules if rule.enabled and id(rule) in losers and id(rule) not in winners
    }


def overlaps(
    rules: Sequence[RuleView],
    rule: RuleView,
    monday: dt.date,
    name_of: Callable[[str], str],
    applies: Applies,
) -> tuple[list[str], list[str]]:
    """(playlists this rule beats, playlists that beat it) in a week it applies.

    ``rules`` is the list as it would be with ``rule`` in its place.
    """
    if rule.months and seasonal_off([rule], monday):
        monday = next_week_for(rule, monday)
    probe = replace(rule, enabled=True)
    listing = [probe if other is rule else other for other in rules]
    if not any(other is probe for other in listing):
        listing.append(probe)
    index = next(at for at, other in enumerate(listing) if other is probe)
    beats: list[str] = []
    beaten: list[str] = []
    for spans in week_spans(listing, monday, rule.connector, applies):
        for span in spans:
            active = ([span.rule] if span.rule is not None else []) + span.losers
            if not any(other is probe for other in active):
                continue
            for other in active:
                if other is probe or other.playlist == probe.playlist:
                    continue
                name = name_of(other.playlist)
                position = next(at for at, candidate in enumerate(listing) if candidate is other)
                bucket = beats if position < index else beaten
                if name not in bucket:
                    bucket.append(name)
    return beats, beaten


def coverage(rule: RuleView, applies: Applies) -> list[list[tuple[int, int]]]:
    """Per weekday, the minutes ``rule`` covers on its own (months and display ignored)."""
    probe = replace(rule, months=frozenset(), connector="", enabled=True)
    monday = dt.date(2026, 9, 28)  # any week: months are ignored
    result = []
    for column in range(7):
        spans = day_spans([probe], monday + dt.timedelta(days=column), column, "", applies)
        result.append([(span.start, span.end) for span in spans if span.rule is probe])
    return result


# -- "Why?": why each rule does or doesn't decide right now --------------------------------


def reason(
    rule: RuleView,
    at: dt.datetime,
    connector: str,
    winner: str | None,
    applies: Applies,
    *,
    mirrored: bool,
) -> tuple[str, str]:
    """(short reason, kind) for one rule; kind is playing, overridden, idle or off.

    ``winner`` is the runtime's rule id for ``connector``; this only says why
    the others are not it.
    """
    if mirrored and rule.connector:
        return "Only while displays run separately", "off"
    if not rule.enabled:
        return "Off", "off"
    if rule.id and rule.id == winner:
        return "Playing", "playing"
    if rule.connector and connector != rule.connector:
        return f"{rule.connector} only", "idle"
    if applies(rule, at, connector):
        return "Overridden", "overridden"
    minute = at.hour * 60 + at.minute
    within, tail = True, False
    if has_window(rule) and rule.start is not None and rule.end is not None:
        if rule.start < rule.end:
            within = rule.start <= minute < rule.end
        else:
            within = minute >= rule.start or minute < rule.end
            tail = minute < rule.end
    calendar = at - dt.timedelta(days=1) if tail else at
    if rule.months and calendar.month not in rule.months:
        return f"{months_text(rule.months)} only", "idle"
    if rule.weekdays and calendar.weekday() not in rule.weekdays:
        return f"{days_text(rule.weekdays)} only", "idle"
    if not within and rule.start is not None and rule.end is not None:
        if rule.start > minute:
            return f"Starts {hhmm(rule.start)}", "idle"
        return f"Ended {hhmm(rule.end)}", "idle"
    return "Not now", "idle"
