"""What the new interface's Schedule and Displays pages need from the app.

The schedule's rules, the default playlist, each display's assignment, the
display mode and the desktop colors' source, and what the runtime reports
about each display. `AppState.scheduling` returns a `Scheduling`, or None
while the app can't offer it; the pages then show their "not in the new
interface yet" placeholders. The real one is `real_schedule.RealScheduling`;
the prototype's demo state is the other.

**One convention for a rule** (`RuleView`), whatever the adapter stores:

* ``weekdays``: Monday is 0 and Sunday 6, as `datetime.weekday` counts; empty
  means every day.
* ``months``: 1 to 12, as `datetime.month` counts; empty means all year.
* ``start``/``end``: minutes past midnight, both or neither. Neither is all
  day. The end is exclusive, and an end before the start wraps past
  midnight; the tail after midnight belongs to the day the window started.
  ``start == end`` is refused: the store reads it as "always", which is never
  what a person dragging or picking a time meant.
* ``connector``: empty for every display; a connector makes the rule that
  display's own, which only applies while displays run separately.

**Priority.** `Scheduling.schedule_rules` is in stored order and the *last*
applicable rule wins. The pages show the highest priority on top, the stored
order reversed (`schedule_model.shown_index`); `Scheduling.shift_rule` steps
in stored order, so +1 is one place higher.

**"Now" and "why" are the runtime's.** `Scheduling.routes` and
`Scheduling.rule_truth` are what the wallpaper service reported (its rule id,
its "until"); nothing here decides which rule is in force now. The pages may
project the calendar forward with `Scheduling.rule_applies`, which uses the
app's own matching.

**Writes** are asynchronous. Each takes ``settled``, called exactly once on
the GTK thread when the write was saved (True), failed or was refused
(False), possibly before the method returns. The adapter reports failures
itself; a page puts its control back on what is durable once the last write
it queued for that control has settled (`wall_in_one.ui.pending_edits`).
Nothing a page shows changes before the store says so. The runtime controls
here (`set_display_rotation`, `use_saved_rotation`, `play_now`) are runtime
commands, never stored: their result arrives with the next status.

GTK-free: data and annotations only.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from wall_in_one.ui.next.state import Route

#: A write's completion: True when it was saved.
Settled = Callable[[bool], None]

MINUTES_IN_A_DAY: Final = 24 * 60


@dataclass(frozen=True, slots=True)
class RuleView:
    """One schedule rule, or a draft of one (``id`` empty), in the convention above."""

    id: str
    #: The playlist's id, as `AppState.playlist` knows it.
    playlist: str
    weekdays: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    start: int | None = None
    end: int | None = None
    connector: str = ""
    enabled: bool = True
    #: What the person called the rule, or None. Shown, never edited here.
    name: str | None = None

    @property
    def timed(self) -> bool:
        """A real time window (not all day)."""
        return self.start is not None and self.end is not None


@dataclass(frozen=True, slots=True)
class RouteView:
    """What one display shows and why, as the runtime reported it."""

    connector: str
    route: Route
    #: The playlist's id and the runtime's name for it.
    playlist_id: str
    playlist: str
    #: The rule that decides it (the runtime's ``schedule_rule_id``), or None.
    rule_id: str | None = None
    #: When the reason ends ("18:00"), only when the runtime said so.
    until: str = ""
    #: A pick of one wallpaper ("Your pick"), not of a playlist.
    quick: bool = False
    #: ``playing``, ``paused`` or ``stopped``.
    playback: str = "playing"
    cycle: bool = False
    cycle_default: bool = False
    #: Cycle was set by hand on this display (not its saved default).
    cycle_manual: bool = False
    shuffle: bool = False
    shuffle_default: bool = False
    shuffle_manual: bool = False


@dataclass(frozen=True, slots=True)
class RuleTruth:
    """One rule as the runtime reported it."""

    #: The schedule chooses it for at least one display right now.
    selected: bool
    #: ...and no pick by hand hides it there.
    in_force: bool


@dataclass(frozen=True, slots=True)
class DisplayInfo:
    """A display the app knows: connected now, or kept for later."""

    connector: str
    model: str = ""
    connected: bool = True
    #: The compositor's layout, in logical pixels, when it is known.
    x: int = 0
    y: int = 0
    width: int = 1920
    height: int = 1080
    placed: bool = False
    scale: float = 1.0
    #: Refresh rate in millihertz, 0 when unknown.
    refresh: int = 0


@dataclass(frozen=True, slots=True)
class ColorSource:
    """Which display Noctalia's one palette follows."""

    #: The saved choice, or empty.
    configured: str = ""
    #: The display it follows right now, or empty when none can.
    effective: str = ""
    #: The saved display is not connected; ``effective`` stands in for it.
    fallback: bool = False


@dataclass(frozen=True, slots=True)
class ForgetPlan:
    """What forgetting a display that is not connected would remove."""

    connector: str
    #: Its playlist for when nothing is scheduled, or empty.
    playlist: str = ""
    #: How many schedule rules are aimed at it.
    rules: int = 0
    #: Desktop colors were saved to follow it (cleared while mirrored).
    colors: bool = False
    #: Why it can't be forgotten now, or empty when it can.
    refused: str = ""


class Scheduling(Protocol):
    """The Schedule and Displays pages' view of the app."""

    # -- reading ---------------------------------------------------------------------
    def clock(self) -> datetime.datetime:
        """Local time now, for the calendar's "now" line and its words."""
        ...

    @property
    def schedule_rules(self) -> Sequence[RuleView]:
        """Every rule, in stored order: the last applicable one wins."""
        ...

    def schedule_rule(self, rule_id: str) -> RuleView | None: ...

    def rule_applies(self, rule: RuleView, at: datetime.datetime, connector: str = "") -> bool:
        """Whether ``rule`` applies at ``at`` on ``connector`` ("" = the shared schedule).

        The app's own matching, for projecting the calendar; never the answer
        to what is in force now (`routes`).
        """
        ...

    def saved_display_mode(self) -> str:
        """``mirrored`` or ``independent`` as saved: what the next compile uses.

        `AppState.display_mode` is the runtime's answer, which follows a
        change only once the service reloads.
        """
        ...

    @property
    def default_playlist(self) -> str:
        """The playlist id that plays when nothing is scheduled or assigned."""
        ...

    def display_assignments(self) -> Mapping[str, str]:
        """Connector -> its own playlist id, including displays not connected now."""
        ...

    def beats_global_rules(self, connector: str) -> bool:
        """Whether ``connector``'s own playlist beats the rules for every display."""
        ...

    def known_displays(self) -> Sequence[DisplayInfo]:
        """Connected displays first, in the compositor's order, then the ones kept for later."""
        ...

    def routes(self) -> Sequence[RouteView]:
        """Each connected display's route, as the runtime last reported it; empty when unknown."""
        ...

    def rule_truth(self, rule_id: str) -> RuleTruth | None:
        """What the runtime reported about one rule, or None when it reported nothing."""
        ...

    def color_source(self) -> ColorSource: ...

    def overrides_apply(self) -> bool:
        """Whether the running service applies "beats the shared rules" (else: after it updates)."""
        ...

    def schedule_blocked(self) -> str:
        """Why rules and display playlists can't change now, or empty."""
        ...

    def settings_blocked(self) -> str:
        """Why the default playlist, the display mode and the colors' display can't change."""
        ...

    def runtime_blocked(self) -> str:
        """Why runtime commands (rotation, play now) are off now, or empty."""
        ...

    def playlist_blocked(self, pid: str) -> str:
        """Why ``pid`` can't be scheduled, assigned or made the default (missing, nothing
        playable), or empty when it can."""
        ...

    # -- the schedule ------------------------------------------------------------------
    def save_rule(self, rule_id: str, draft: RuleView, settled: Settled | None = None) -> None:
        """Change rule ``rule_id`` to ``draft``, keeping its place, name and switch; or,
        with ``rule_id`` empty, add ``draft`` with the highest priority."""
        ...

    def remove_rule(self, rule_id: str, settled: Settled | None = None) -> None: ...

    def copy_rule(self, rule_id: str, settled: Settled | None = None) -> None:
        """A copy just above the rule (one place higher)."""
        ...

    def enable_rule(self, rule_id: str, enabled: bool, settled: Settled | None = None) -> None: ...

    def shift_rule(self, rule_id: str, step: int, settled: Settled | None = None) -> None:
        """Move from its latest saved place by ``step`` in stored order (+1 = higher)."""
        ...

    def choose_default_playlist(self, pid: str, settled: Settled | None = None) -> None: ...

    # -- displays ---------------------------------------------------------------------
    def assign_display(self, connector: str, pid: str, settled: Settled | None = None) -> None:
        """``connector``'s own playlist for when nothing is scheduled; "" follows the default."""
        ...

    def set_beats_global_rules(
        self, connector: str, enabled: bool, settled: Settled | None = None
    ) -> None:
        """Let an assigned display's own playlist beat the rules for every display."""
        ...

    def choose_display_mode(self, mode: str, settled: Settled | None = None) -> None:
        """``mirrored`` or ``independent``; independent needs a connected display for colors."""
        ...

    def choose_color_source(self, connector: str, settled: Settled | None = None) -> None: ...

    def forget_plan(self, connector: str) -> ForgetPlan: ...

    def forget_display(self, connector: str, settled: Settled | None = None) -> None:
        """Forget a display that is not connected: its playlist and the rules aimed at it."""
        ...

    # -- runtime commands on one display (never stored) ---------------------------------
    def set_display_rotation(
        self, connector: str, *, cycle: bool | None = None, shuffle: bool | None = None
    ) -> None:
        """Turn Cycle or Shuffle on or off on ``connector`` until its saved defaults are used."""
        ...

    def use_saved_rotation(self, connector: str) -> None:
        """Drop ``connector``'s Cycle and Shuffle choices for its saved defaults."""
        ...

    def play_now(self, connector: str, pid: str) -> None:
        """Play ``pid`` now on ``connector`` ("" = every display); "" ``pid`` resumes."""
        ...
