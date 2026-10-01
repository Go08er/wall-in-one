"""Schedule logic shared by the calendar, the rules list and the rule editor.

Nothing here decides *whether* a rule applies on its own: every answer comes
from ``state.rule_matches`` / ``state.resolve`` (the runtime's semantics),
evaluated once per stretch of time in which no rule can change its mind.
This module only turns those answers into spans, colors and short words.
"""

from __future__ import annotations

import colorsys
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise

from .. import art
from ..catalog import DAYS, MONTHS, MONTHS_LONG
from ..models import Playlist, Rule
from ..state import rule_matches

WEEKDAYS = [0, 1, 2, 3, 4]
WEEKEND = [5, 6]
DAY_MINUTES = 24 * 60

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def minutes(text: str | None) -> int | None:
    if not text:
        return None
    hours, mins = text.split(":")
    return int(hours) * 60 + int(mins)


def hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def has_window(rule: Rule) -> bool:
    """A real time window. start == end means "always", the same as all day."""
    return bool(rule.start and rule.end and rule.start != rule.end)


def wraps(rule: Rule) -> bool:
    return has_window(rule) and minutes(rule.start) > minutes(rule.end)


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def _runs(values: list[int], size: int) -> list[list[int]]:
    """Consecutive runs, allowing a run to wrap from the end of the cycle to its start."""
    chosen = sorted(set(values))
    if not chosen:
        return []
    # Start after a gap so a run like Sat–Mon is kept together.
    start = next((v for v in chosen if (v - 1) % size not in chosen), chosen[0])
    ordered = [v for v in chosen if v >= start] + [v for v in chosen if v < start]
    runs: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value == (runs[-1][-1] + 1) % size:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def days_text(days: list[int]) -> str:
    if not days or len(set(days)) == 7:
        return "Every day"
    if len(days) == 1:
        return DAYS[days[0]]
    parts = []
    for run in _runs(days, 7):
        if len(run) >= 2 and not (len(run) == 2 and len(days) > 3):
            parts.append(f"{DAYS[run[0]]}–{DAYS[run[-1]]}")
        else:
            parts.extend(DAYS[v] for v in run)
    return ", ".join(parts)


def months_text(months: list[int]) -> str:
    if not months or len(set(months)) == 12:
        return "All year"
    if len(months) == 1:
        return MONTHS_LONG[months[0]]
    parts = []
    for run in _runs(months, 12):
        if len(run) >= 3:
            parts.append(f"{MONTHS[run[0]]}–{MONTHS[run[-1]]}")
        else:
            parts.extend(MONTHS[v] for v in run)
    return ", ".join(parts)


def time_text(rule: Rule, overnight: bool = True) -> str:
    if not has_window(rule):
        return "All day"
    text = f"{rule.start}–{rule.end}"
    if overnight and wraps(rule):
        text += " (overnight)"
    return text


def summary(rule: Rule, display: bool = True, overnight: bool = True) -> str:
    """'Mon–Fri · 09:00–17:00 · HDMI-A-1', 'Every day · 18:00–07:00 (overnight)', 'All day · December'."""
    parts: list[str] = []
    if has_window(rule):
        parts += [days_text(rule.days), time_text(rule, overnight)]
    elif rule.days:
        parts += [days_text(rule.days), "All day"]
    else:
        parts.append("All day" if rule.months else "Always")
    if rule.months:
        parts.append(months_text(rule.months))
    if display and rule.display:
        parts.append(rule.display)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Colors: one per playlist, taken from its pictures, kept distinct.
# ---------------------------------------------------------------------------

# libadwaita's accent colors: readable in light and dark, white text on top.
PALETTE = {
    "green": "#3a944a",
    "purple": "#9141ac",
    "yellow": "#c88800",
    "blue": "#3584e4",
    "teal": "#2190a4",
    "pink": "#d56199",
    "orange": "#ed5b00",
    "red": "#e62d42",
}
FALLBACK_COLOR = "#6f8396"  # slate


def _hue(hex_color: str) -> float:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[0]


def _mood_hue(state, playlist: Playlist) -> float:
    """Day playlists are colored by their land, night playlists by their sky."""
    if not playlist.entries:
        return 0.6
    first = state.wallpaper(playlist.entries[0])
    look = art.look_for(*first.key)
    if first.night:
        return colorsys.rgb_to_hls(*look.sky_top)[0]
    return look.hue


_COLORS: dict[tuple[str, ...], dict[str, str]] = {}


def playlist_colors(state) -> dict[str, str]:
    playlists = state.playlists
    key = tuple(p.id for p in playlists)
    if key not in _COLORS:
        taken: dict[str, str] = {}
        free = dict(PALETTE)
        for playlist in playlists:
            if playlist.automatic:
                taken[playlist.id] = FALLBACK_COLOR
                continue
            if not free:
                free = dict(PALETTE)
            hue = _mood_hue(state, playlist)
            name = min(free, key=lambda n: min(abs(_hue(free[n]) - hue), 1 - abs(_hue(free[n]) - hue)))
            taken[playlist.id] = free.pop(name)
        _COLORS[key] = taken
    return _COLORS[key]


def color_for(state, pid: str) -> str:
    return playlist_colors(state).get(pid, FALLBACK_COLOR)


def rgb(hex_color: str) -> tuple[float, float, float]:
    value = hex_color.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Spans: what plays when, for a week, as the runtime would decide it.
# ---------------------------------------------------------------------------


@dataclass
class Span:
    day: int  # column 0..6 (Mon..Sun)
    start: int  # minutes from midnight
    end: int  # exclusive, up to 1440
    rule: Rule | None  # winner; None = the fallback playlist
    losers: list[Rule] = field(default_factory=list)  # matching but overridden (low → high)

    @property
    def top_loser(self) -> Rule | None:
        return self.losers[-1] if self.losers else None


@dataclass
class DisplayException:
    """A rule for one display, shown on the 'All displays' calendar as a side block."""

    day: int
    start: int
    end: int
    rule: Rule
    wins: bool
    over: Rule | None = None  # what it replaces on that display


def _breakpoints(rules: list[Rule]) -> list[int]:
    points = {0, DAY_MINUTES}
    for rule in rules:
        if has_window(rule):
            points.add(minutes(rule.start))
            points.add(minutes(rule.end))
    return sorted(points)


def _at(day: dt.date, minute: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60)


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for span in spans:
        last = merged[-1] if merged else None
        if (
            last
            and last.rule is span.rule
            and last.end == span.start
            and [id(r) for r in last.losers] == [id(r) for r in span.losers]
        ):
            last.end = span.end
        else:
            merged.append(span)
    return merged


def day_spans(rules: list[Rule], day: dt.date, column: int, display: str | None) -> list[Span]:
    """Pieces of one day; inside each piece no rule changes its answer."""
    points = _breakpoints(rules)
    spans = []
    for start, end in pairwise(points):
        at = _at(day, start)
        matching = [rule for rule in rules if rule_matches(rule, at, display)]
        spans.append(Span(column, start, end, matching[-1] if matching else None, matching[:-1]))
    return _merge(spans)


def week_spans(rules: list[Rule], monday: dt.date, display: str | None) -> list[list[Span]]:
    return [day_spans(rules, monday + dt.timedelta(days=i), i, display) for i in range(7)]


def week_exceptions(rules: list[Rule], monday: dt.date) -> list[DisplayException]:
    """Display-only rules, for the 'All displays' view."""
    found: list[DisplayException] = []
    targeted = [rule for rule in rules if rule.display and rule.enabled]
    if not targeted:
        return found
    points = _breakpoints(rules)
    for column in range(7):
        day = monday + dt.timedelta(days=column)
        for rule in targeted:
            pieces: list[DisplayException] = []
            for start, end in pairwise(points):
                at = _at(day, start)
                if not rule_matches(rule, at, rule.display):
                    continue
                matching = [r for r in rules if rule_matches(r, at, rule.display)]
                wins = matching[-1] is rule
                over = None
                if wins and len(matching) > 1:
                    over = matching[-2]
                last = pieces[-1] if pieces else None
                if last and last.end == start and last.wins == wins:
                    last.end = end
                else:
                    pieces.append(DisplayException(column, start, end, rule, wins, over))
            found += pieces
    return found


def seasonal_off(rules: list[Rule], monday: dt.date) -> list[Rule]:
    """Enabled rules limited to months that this week doesn't touch."""
    months = {(monday + dt.timedelta(days=i)).month - 1 for i in range(7)}
    return [rule for rule in rules if rule.enabled and rule.months and not months & set(rule.months)]


def next_week_for(rule: Rule, monday: dt.date) -> dt.date:
    """Monday of the first week, from this one on, in which ``rule``'s months apply."""
    probe = monday
    for _ in range(60):
        days = [probe + dt.timedelta(days=i) for i in range(7)]
        if any(day.month - 1 in rule.months for day in days):
            return probe
        probe += dt.timedelta(days=7)
    return monday


def never_wins(rules: list[Rule], rule: Rule, monday: dt.date, connectors: list[str]) -> bool:
    """True when the rule applies this week but higher rules cover every minute of it."""
    if not rule.enabled:
        return False
    displays = [rule.display] if rule.display else [None, *connectors]
    applies = False
    for display in displays:
        for spans in week_spans(rules, monday, display):
            for span in spans:
                if span.rule is rule:
                    return False
                if rule in span.losers:
                    applies = True
    return applies


def overlaps(
    rules: list[Rule], rule: Rule, monday: dt.date, name_of: Callable[[str], str]
) -> tuple[list[str], list[str]]:
    """(names of the playlists this rule overrides, of those that override it) during a
    week it applies. ``name_of`` turns a playlist id into its name (state.playlist_name)."""
    if rule.months and seasonal_off([rule], monday):
        monday = next_week_for(rule, monday)
    beats: list[str] = []
    beaten: list[str] = []
    probe = Rule(rule.id, rule.playlist, rule.days, rule.start, rule.end, rule.months, rule.display, True)
    listing = [probe if r is rule else r for r in rules]
    if rule not in rules:
        listing.append(probe)
    index = listing.index(probe)
    for spans in week_spans(listing, monday, rule.display or None):
        for span in spans:
            active = ([span.rule] if span.rule else []) + span.losers
            if probe not in active:
                continue
            for other in active:
                if other is probe or other.playlist == probe.playlist:
                    continue
                name = name_of(other.playlist)
                bucket = beats if listing.index(other) < index else beaten
                if name not in bucket:
                    bucket.append(name)
    return beats, beaten


def coverage(rule: Rule) -> list[list[tuple[int, int]]]:
    """Per weekday, the minutes this rule covers on its own (months and display ignored)."""
    probe = Rule(rule.id, rule.playlist, rule.days, rule.start, rule.end, [], "", True)
    monday = dt.date(2026, 9, 28)
    result = []
    for column in range(7):
        spans = day_spans([probe], monday + dt.timedelta(days=column), column, None)
        result.append([(s.start, s.end) for s in spans if s.rule is probe])
    return result


# ---------------------------------------------------------------------------
# "Why?" — the reason each rule does or doesn't decide right now.
# ---------------------------------------------------------------------------


def reason(rule: Rule, at: dt.datetime, display: str | None, winner: Rule | None) -> tuple[str, str]:
    """(short reason, kind) where kind is playing | overridden | idle | off."""
    if not rule.enabled:
        return "Off", "off"
    if rule.display and display != rule.display:
        return f"{rule.display} only", "idle"
    minute = at.hour * 60 + at.minute
    within, tail = True, False
    if has_window(rule):
        start, end = minutes(rule.start), minutes(rule.end)
        if start < end:
            within = start <= minute < end
        else:
            within = minute >= start or minute < end
            tail = minute < end
    calendar = at - dt.timedelta(days=1) if tail else at
    if rule.months and calendar.month - 1 not in rule.months:
        return f"{months_text(rule.months)} only", "idle"
    if rule.days and calendar.weekday() not in rule.days:
        return f"{days_text(rule.days)} only", "idle"
    if not within:
        start = minutes(rule.start)
        return (f"Starts {rule.start}", "idle") if start > minute else (f"Ended {rule.end}", "idle")
    if rule is winner:
        return "Playing", "playing"
    return "Overridden", "overridden"
