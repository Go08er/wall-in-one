"""The real `Scheduling`: the schedule, display and settings stores, and the runtime's status.

**Reads.** Rules come from the session's schedule store (`library.schedules`)
in stored order, converted to `RuleView` (weekdays Monday=0, months 1-12,
minutes; the store already holds them so, and writes them as ``mon``..``sun``
and ``HH:MM``). Assignments and the "beats the shared rules" opt-in come from
the display store; the default playlist, display mode and desktop colors'
display from the settings. What each display shows and why (`routes`,
`rule_truth`) is the runtime's own status, never resolved here. Known
displays are GTK's monitors (layout, scale, refresh rate) merged with the
runtime's connected displays, then every display something is saved for:
an assignment, a rule, the colors' choice.

**Writes** go through the application's ordered authoring lane
(`authoring_action_async`): rules and assignments are store transactions
whose completion republishes the runtime configuration (``schedule_edited``
or ``runtime_config_changed``, as the classic Schedules page does); the
default, the mode and the colors' display are settings edits
(`update_settings_async`); the mode and the colors' display follow the same
rule as Settings → Displays (`displays_settings`). Each write settles exactly once: saved, failed
(reported, "nothing changed"), or refused before it was queued. Forget
Display is the one write that touches several stores: when a later one
fails it says exactly what it removed and what is still saved, never
"nothing changed", and publishes what committed (`forget_display`). A playlist
that would compile to nothing is refused for a rule, an assignment or the
default, both at the gesture and again when the write reaches the head of
the lane. "Beats the shared rules" is the display store's version-2 field;
the compiler puts it in ``runtime-overrides.toml`` and never in
``runtime.toml``.

**Runtime commands** (per-display Cycle and Shuffle, "Use saved defaults",
play now) are the classic window's runtime verbs. They are not stored: the
runtime keeps them until its saved defaults are asked for, and the next
status shows the result.

**Change notification.** `adopt` runs with every status publication the
adapter takes and names the topics whose answers changed: when a
route, its rule or its rotation did (topic ``routes``), ``schedule`` and ``displays`` when the
stores or settings did (including edits made by ``ctl`` while the window is
open). GTK's monitor list is followed for hotplug.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, TypeVar

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk

from wall_in_one import config, display_policy, runtime_config
from wall_in_one.library import displays as display_store
from wall_in_one.library import schedules
from wall_in_one.library.playlists import (
    DISPLAY_QUICK_CHOICE_ID_PREFIX,
    DISPLAY_QUICK_CHOICE_NAME_PREFIX,
)
from wall_in_one.session import QUICK_CHOICE_ID, SAVED_PLAYLISTS, Session
from wall_in_one.ui import runtime_truth
from wall_in_one.ui.next import schedule_model
from wall_in_one.ui.next.displays_settings import settings_edit
from wall_in_one.ui.next.schedule_state import (
    ColorSource,
    DisplayInfo,
    ForgetPlan,
    RouteView,
    RuleTruth,
    RuleView,
    Settled,
)
from wall_in_one.ui.next.state import Route
from wall_in_one.ui.status_model import RuntimeStatusModel, RuntimeStatusView

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ColorsForgotten:
    """What Forget Display's last step did to the desktop colors' display.

    `ScheduleBackend.prepare_colors_forget` decides it at the head of the
    authoring lane and writes it on the worker; `adopt_colors_forget` takes it
    on GTK. Nothing written and nothing ``kept`` means a newer choice won.
    """

    #: ``settings.toml`` as this step wrote it; None when it wrote nothing.
    settings: config.Settings | None = None
    #: Why the colors still follow the forgotten display, when they do.
    kept: str = ""


@dataclass(frozen=True, slots=True)
class _Forgotten:
    """What Forget Display committed: the rules, the playlist, the colors, and why it stopped."""

    #: The rules removed, in one transaction.
    rules: tuple[schedules.Rule, ...]
    #: The playlist unassigned ("" when there was none); None: that step failed.
    playlist: str | None
    error: str = ""
    #: The colors' step, when it ran; None when it wasn't needed or didn't get to run.
    colors: ColorsForgotten | None = None
    #: Why the colors' step failed after the stores were saved.
    colors_error: str = ""


def _removed_words(outcome: _Forgotten, label: Callable[[str], str | None]) -> list[str]:
    """Its playlist and its rules, as removed, for a sentence."""
    words = []
    if outcome.playlist:
        name = label(outcome.playlist) or outcome.playlist
        words.append(f"its own playlist (\u201c{name}\u201d)")
    if outcome.rules:
        count = len(outcome.rules)
        words.append(f"its {count} schedule rule{'s' if count != 1 else ''}")
    return words or ["nothing of its own"]


_ROUTES: Final[dict[str, Route]] = {
    "manual": "pick",
    "schedule": "schedule",
    "assignment": "display",
    "default": "default",
}
#: Converted rules kept for `rule_applies`; cleared past this many.
_MATCHER_CACHE: Final = 1024


class ScheduleBackend(Protocol):
    """What `RealScheduling` uses of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def status_model(self) -> RuntimeStatusModel: ...

    @property
    def settings(self) -> config.Settings: ...

    @property
    def requested_settings(self) -> config.Settings:
        """What is saved with every queued settings edit applied."""
        ...

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]: ...

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool: ...

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool: ...

    def schedule_edited(self) -> None: ...

    def runtime_config_changed(self) -> None: ...

    def require_current_library(self) -> None:
        """Refused while the library may name files that are gone (`LibraryNotCurrentError`)."""
        ...

    def prepare_colors_forget(self, connector: str) -> Callable[[], ColorsForgotten]:
        """At the head of the authoring lane: Forget Display's colors step, for its worker."""
        ...

    def adopt_colors_forget(self, result: ColorsForgotten | None) -> None:
        """On GTK: take what that step saved, or put back what it projected."""
        ...

    def runtime_action_async(self, verb: str, argument: str | None = None) -> bool: ...

    def runtime_action_on_async(
        self, connector: str, verb: str, argument: str | None = None
    ) -> bool: ...

    def reset_display_modes_on_async(self, connector: str) -> bool: ...

    def activate_playlist_async(self, reference: str) -> bool: ...

    def activate_playlist_on_async(self, connector: str, reference: str) -> bool: ...

    def resume_schedule_async(self) -> bool: ...

    def resume_schedule_on_async(self, connector: str) -> bool: ...

    def window_report(self, message: str) -> None: ...


class Notified(Protocol):
    """What `RealScheduling` needs of the adapter that owns it."""

    def emit_changed(self, *topics: str) -> None: ...


# -- conversions ------------------------------------------------------------------------


def _is_quick_choice(playlist_id: str) -> bool:
    return playlist_id == QUICK_CHOICE_ID or playlist_id.startswith(DISPLAY_QUICK_CHOICE_ID_PREFIX)


def rule_view(rule: schedules.Rule) -> RuleView:
    """A stored rule in the pages' convention (the store's own numbers)."""
    return RuleView(
        id=rule.id,
        playlist=rule.playlist,
        weekdays=frozenset(rule.weekdays),
        months=frozenset(rule.months),
        start=rule.start,
        end=rule.end,
        connector=rule.connector,
        enabled=rule.enabled,
        name=rule.name,
    )


def stored_rule(view: RuleView) -> schedules.Rule:
    """The store's rule for ``view`` (for matching only; never saved as is)."""
    return schedules.Rule(
        id=view.id or "draft",
        playlist=view.playlist,
        connector=view.connector,
        months=frozenset(view.months),
        weekdays=frozenset(view.weekdays),
        start=view.start,
        end=view.end,
        enabled=view.enabled,
        name=view.name,
    )


def store_arguments(view: RuleView) -> dict[str, Any]:
    """`schedules.Store.add`/``update`` keyword arguments for ``view``."""
    timed = view.start is not None and view.end is not None
    return {
        "months": sorted(view.months),
        "weekdays": [schedules.WEEKDAY_NAMES[day] for day in sorted(view.weekdays)],
        "start": schedules.format_time(view.start) if timed and view.start is not None else "",
        "end": schedules.format_time(view.end) if timed and view.end is not None else "",
        "connector": view.connector,
    }


def draft_problem(view: RuleView) -> str:
    """Why ``view`` can't be saved as a rule, or empty (the playlist is checked apart)."""
    if not view.playlist:
        return "A rule needs a playlist"
    if any(day not in range(7) for day in view.weekdays):
        return "A weekday is Monday (0) to Sunday (6)"
    if any(month not in range(1, 13) for month in view.months):
        return "A month is 1 to 12"
    return schedule_model.window_problem(view.start, view.end)


# -- the adapter ------------------------------------------------------------------------


class RealScheduling:
    """`Scheduling` over the application's stores, settings and runtime status."""

    def __init__(
        self,
        backend: ScheduleBackend,
        owner: Notified,
        runtime_off: Callable[[], str],
    ) -> None:
        self._backend = backend
        self._owner = owner
        #: Why runtime commands are off now (the playback controls' answer).
        self._runtime_off = runtime_off
        self._matchers: dict[RuleView, schedules.Rule] = {}
        self._routes_key: object = None
        self._schedule_key: object = None
        self._displays_key: object = None
        self._monitors: Any = None
        self._monitor_handler = 0

    # -- lifetime --------------------------------------------------------------------
    def listen(self) -> None:
        """Follow GTK's monitors (hotplug changes what is connected and where)."""
        if self._monitor_handler:
            return
        display = Gdk.Display.get_default()
        if display is None:
            return
        self._monitors = display.get_monitors()
        self._monitor_handler = self._monitors.connect(
            "items-changed", lambda *_args: self._monitors_changed()
        )

    def close(self) -> None:
        if self._monitor_handler and self._monitors is not None:
            self._monitors.disconnect(self._monitor_handler)
        self._monitor_handler = 0
        self._monitors = None

    def _monitors_changed(self) -> None:
        self._displays_key = None
        self._owner.emit_changed("displays")

    # -- following the app ------------------------------------------------------------
    @property
    def _session(self) -> Session:
        return self._backend.session

    @property
    def _view(self) -> RuntimeStatusView:
        return self._backend.status_model.view

    def _truth(self) -> runtime_truth.RuntimeTruth | None:
        view = self._view
        return view.truth if view.service == "running" else None

    def adopt(self, view: RuntimeStatusView) -> list[str]:
        """The topics whose answers changed since the last call (see the module)."""
        topics: list[str] = []
        truth = view.truth if view.service == "running" else None
        routes = self._read_routes(truth)
        rules = truth.schedules if truth is not None else ()
        routes_key = (routes, rules, view.service)
        if routes_key != self._routes_key:
            self._routes_key = routes_key
            topics.append("routes")
        settings = self._backend.settings
        session = self._session
        schedule_key = (session.schedules.rules, settings.active_playlist)
        if schedule_key != self._schedule_key:
            self._schedule_key = schedule_key
            topics.append("schedule")
        displays_key = (
            session.displays.all(),
            tuple(c for c, _p in session.displays.all() if session.displays.beats_global_rules(c)),
            settings.display_mode,
            settings.theme_source_connector,
            truth.theme_source if truth is not None else None,
            runtime_config.runtime_applies_overrides(view.status),
            tuple((d.connector, d.connected) for d in truth.displays) if truth else (),
        )
        if displays_key != self._displays_key:
            self._displays_key = displays_key
            topics.append("displays")
        return topics

    # -- Scheduling: reading -----------------------------------------------------------
    def clock(self) -> datetime.datetime:
        return datetime.datetime.now()

    @property
    def schedule_rules(self) -> Sequence[RuleView]:
        return tuple(rule_view(rule) for rule in self._session.schedules.rules)

    def schedule_rule(self, rule_id: str) -> RuleView | None:
        return next((rule for rule in self.schedule_rules if rule.id == rule_id), None)

    def rule_applies(self, rule: RuleView, at: datetime.datetime, connector: str = "") -> bool:
        """`schedules.Rule.matches`: the app's own semantics, for projection only."""
        stored = self._matchers.get(rule)
        if stored is None:
            if len(self._matchers) >= _MATCHER_CACHE:
                self._matchers.clear()
            stored = self._matchers[rule] = stored_rule(rule)
        return stored.matches(at, connector)

    def saved_display_mode(self) -> str:
        return self._backend.settings.display_mode

    @property
    def default_playlist(self) -> str:
        """The default in force, as an id the page can name.

        A default saved by name is its playlist
        (`runtime_config.default_playlist_id`). One left out because it names
        nothing and ``settings.toml`` can't be changed to clear it
        (`Session.leave_out_default`) is not in force: "All media" plays, and
        that is what is shown, never a row with no name. Any other reference
        no playlist answers to is passed on as saved; the start repairs it.
        """
        saved = self._backend.settings.active_playlist
        if not saved:
            return runtime_config.FALLBACK_PLAYLIST_ID
        named = runtime_config.default_playlist_id(saved, self._session)
        if named:
            return named
        if saved == self._session.left_out_default:
            return runtime_config.FALLBACK_PLAYLIST_ID
        return saved

    def display_assignments(self) -> Mapping[str, str]:
        return dict(self._session.displays.all())

    def beats_global_rules(self, connector: str) -> bool:
        return self._session.displays.beats_global_rules(connector)

    def known_displays(self) -> Sequence[DisplayInfo]:
        found: list[DisplayInfo] = list(_monitor_displays())
        seen = {display.connector for display in found}
        truth = self._truth()
        if truth is not None and truth.status_version == 2:
            for record in truth.displays:
                if record.connected and record.connector not in seen:
                    found.append(DisplayInfo(record.connector))
                    seen.add(record.connector)
        session = self._session
        kept = {connector for connector, _playlist in session.displays.all()}
        kept.update(rule.connector for rule in session.schedules.rules if rule.connector)
        if self._backend.settings.theme_source_connector:
            kept.add(self._backend.settings.theme_source_connector)
        if truth is not None:
            kept.update(record.connector for record in truth.displays if not record.connected)
        for connector in sorted(kept - seen):
            found.append(DisplayInfo(connector, connected=False))
        return tuple(found)

    def _read_routes(self, truth: runtime_truth.RuntimeTruth | None) -> tuple[RouteView, ...]:
        if truth is None:
            return ()
        if truth.status_version == 2:
            return tuple(
                RouteView(
                    connector=display.connector,
                    route=_ROUTES.get(display.route_source, "default"),
                    playlist_id=display.playlist_id,
                    playlist=display.playlist,
                    rule_id=display.schedule_rule_id,
                    until="" if display.route_source == "manual" else display.until or "",
                    quick=_is_quick_choice(display.playlist_id),
                    playback=display.playback_state,
                    cycle=display.cycle_enabled,
                    cycle_default=display.cycle_default,
                    cycle_manual=display.cycle_source == "manual",
                    shuffle=display.shuffle,
                    shuffle_default=display.shuffle_default,
                    shuffle_manual=display.shuffle_source == "manual",
                )
                for display in truth.displays
                if display.connected
            )
        return (
            RouteView(
                connector="",
                route="pick" if truth.is_manual else "schedule",
                playlist_id=truth.playlist_id,
                playlist=truth.playlist,
                rule_id=truth.schedule_rule_id,
                quick=_is_quick_choice(truth.playlist_id),
            ),
        )

    def routes(self) -> Sequence[RouteView]:
        return self._read_routes(self._truth())

    def rule_truth(self, rule_id: str) -> RuleTruth | None:
        truth = self._truth()
        record = truth.schedule_rule(rule_id) if truth is not None else None
        if record is None:
            return None
        return RuleTruth(record.selected, record.in_force)

    def color_source(self) -> ColorSource:
        truth = self._truth()
        if truth is not None and truth.theme_source is not None:
            reported = truth.theme_source
            return ColorSource(reported.configured, reported.effective or "", reported.fallback)
        connected = [display.connector for display in self.known_displays() if display.connected]
        resolved = display_policy.resolve_theme_source(
            self._backend.settings.theme_source_connector, connected
        )
        return ColorSource(resolved.configured, resolved.effective, resolved.is_fallback)

    def overrides_apply(self) -> bool:
        return runtime_config.runtime_applies_overrides(self._view.status)

    def schedule_blocked(self) -> str:
        newer = self._session.newer_version_files()
        if not newer:
            return ""
        names = newer[0] if len(newer) == 1 else ", ".join(newer[:-1]) + f" and {newer[-1]}"
        verb = "is" if len(newer) == 1 else "are"
        return (
            f"Schedule and display changes are off while {names} {verb} from a newer "
            "version of Wall-in-One"
        )

    def settings_blocked(self) -> str:
        unknown = self._backend.settings_unknown_keys
        if unknown:
            return f"Settings are read-only: {config.read_only_message(unknown)}"
        return self.schedule_blocked()

    def runtime_blocked(self) -> str:
        return self._runtime_off()

    def playlist_label(self, pid: str) -> str | None:
        """Any authored playlist's name; the hidden "Your pick" ones as the window says it."""
        playlist = self._session.playlists.get(pid)
        if playlist is None:
            return None
        if pid == QUICK_CHOICE_ID:
            return "Your pick"
        if pid.startswith(DISPLAY_QUICK_CHOICE_ID_PREFIX):
            connector = playlist.name.removeprefix(DISPLAY_QUICK_CHOICE_NAME_PREFIX)
            return f"Your pick \u00b7 {connector}" if connector != playlist.name else "Your pick"
        return playlist.name

    def playlist_blocked(self, pid: str) -> str:
        """The one rule every page uses (`runtime_config.use_refusal`), on what is saved now."""
        return runtime_config.use_refusal(pid, self._session)

    # -- writing: the shared shape --------------------------------------------------------
    def _settle(self, settled: Settled | None, saved: bool) -> None:
        if settled is not None:
            settled(saved)

    def _refuse(self, settled: Settled | None, message: str, *topics: str) -> None:
        # The pages and the waiting control first (AB-6): the report reaches
        # the window and can raise, and must not leave a queued choice showing.
        self._owner.emit_changed(*topics)
        self._settle(settled, False)
        self._backend.window_report(message)

    def _store_write(
        self,
        work: Callable[[], object],
        *,
        what: str,
        topics: tuple[str, ...],
        publish: Callable[[], None],
        settled: Settled | None,
        recheck: Callable[[Session], str] | None = None,
    ) -> None:
        """One store transaction on the authoring lane, settled exactly once.

        ``recheck`` says why the write may not be made as the session it is
        given stands (a playlist that can't be put into use), or nothing. It
        is asked at the head of the lane of what this window holds, and again
        on the worker, just before the write, of what is saved then
        (`runtime_config.judged_on_what_is_saved`): another program may have
        emptied the playlist since this window opened.
        """
        blocked = self.schedule_blocked()
        if blocked:
            self._refuse(settled, blocked, *topics)
            return
        reported = False

        def prepare() -> Callable[[], object]:
            # At the head of the lane: the playlist may have emptied since,
            # and whether it can be put into use is judged by the library,
            # never by one the app knows is stale.
            if recheck is None:
                return work
            self._backend.require_current_library()
            asked = recheck

            def refuse(session: Session) -> None:
                problem = asked(session)
                if problem:
                    raise ValueError(problem)

            # Whether the playlist has something to play; the store written
            # to has no say in that, and repairs its own damaged file.
            return runtime_config.judged_on_what_is_saved(
                refuse, work, self._session, needs=(SAVED_PLAYLISTS,)
            )

        def finish(_result: object) -> None:
            # Saved: settled first, so nothing below that can raise (they reach
            # the window) leaves the control waiting.
            self._settle(settled, True)
            publish()
            self._owner.emit_changed(*topics)

        def failed(error: str) -> None:
            nonlocal reported
            reported = True
            self._settle(settled, False)  # settled first: what follows reaches the window
            self._owner.emit_changed(*topics)  # and the pages, before the report
            self._backend.window_report(f"{what} was not saved; nothing changed: {error}")

        accepted = self._backend.authoring_action_async(
            work, finish, prepare=prepare, failure=failed
        )
        if not accepted and not reported:
            # Refused without a report (shutting down, or authoring not open yet).
            self._owner.emit_changed(*topics)
            self._settle(settled, False)

    def _settings_write(
        self,
        *,
        what: str,
        topics: tuple[str, ...],
        settled: Settled | None,
        after: Callable[[], None] | None = None,
        **changes: Any,
    ) -> None:
        """One settings edit, settled exactly once."""
        blocked = self.settings_blocked()
        if blocked:
            self._refuse(settled, blocked, *topics)
            return
        reported = False

        def saved(_settings: config.Settings) -> None:
            # Saved: settled first, before what can raise (it reaches the window).
            self._settle(settled, True)
            if after is not None:
                after()
            self._owner.emit_changed(*topics)

        def failed(error: str) -> None:
            nonlocal reported
            reported = True
            self._settle(settled, False)  # settled first: what follows reaches the window
            self._owner.emit_changed(*topics)  # and the pages, before the report
            self._backend.window_report(f"{what} was not saved; nothing changed: {error}")

        accepted = self._backend.update_settings_async(on_success=saved, on_error=failed, **changes)
        if not accepted and not reported:
            self._owner.emit_changed(*topics)
            self._settle(settled, False)

    # -- Scheduling: the schedule -----------------------------------------------------------
    def save_rule(self, rule_id: str, draft: RuleView, settled: Settled | None = None) -> None:
        problem = draft_problem(draft) or self.playlist_blocked(draft.playlist)
        if problem:
            self._refuse(settled, f"The rule was not saved: {problem}", "schedule")
            return
        store = self._session.schedules
        arguments = store_arguments(draft)
        enabled = draft.enabled

        def add() -> object:
            # One save: the rule, switched as asked, last (the highest priority).
            return store.add(draft.playlist, enabled=enabled, **arguments)

        def update() -> object:
            # One save: its times, days, months, display, playlist and switch.
            return store.update(rule_id, draft.playlist, enabled=enabled, **arguments)

        self._store_write(
            update if rule_id else add,
            what="The rule",
            topics=("schedule",),
            publish=self._backend.schedule_edited,
            settled=settled,
            recheck=lambda session: runtime_config.use_refusal(draft.playlist, session),
        )

    def remove_rule(self, rule_id: str, settled: Settled | None = None) -> None:
        store = self._session.schedules
        self._store_write(
            lambda: store.remove(rule_id),
            what="Deleting the rule",
            topics=("schedule",),
            publish=self._backend.schedule_edited,
            settled=settled,
        )

    def copy_rule(self, rule_id: str, settled: Settled | None = None) -> None:
        original = self.schedule_rule(rule_id)
        if original is None:
            self._refuse(settled, "That rule no longer exists", "schedule")
            return
        store = self._session.schedules
        self._store_write(
            # One save: the copy, switched like the original, just above it.
            lambda: store.copy(rule_id),
            what="The copy of the rule",
            topics=("schedule",),
            publish=self._backend.schedule_edited,
            settled=settled,
            # The rule as the session asked holds it: the copy is of that one.
            recheck=lambda session: runtime_config.use_refusal(
                next(
                    (rule.playlist for rule in session.schedules.rules if rule.id == rule_id),
                    original.playlist,
                ),
                session,
            ),
        )

    def enable_rule(self, rule_id: str, enabled: bool, settled: Settled | None = None) -> None:
        store = self._session.schedules
        self._store_write(
            lambda: store.set_enabled(rule_id, enabled),
            what="Turning the rule " + ("on" if enabled else "off"),
            topics=("schedule",),
            publish=self._backend.schedule_edited,
            settled=settled,
        )

    def shift_rule(self, rule_id: str, step: int, settled: Settled | None = None) -> None:
        store = self._session.schedules
        self._store_write(
            lambda: store.move_relative(rule_id, step),
            what="The rule's priority",
            topics=("schedule",),
            publish=self._backend.schedule_edited,
            settled=settled,
        )

    def choose_default_playlist(self, pid: str, settled: Settled | None = None) -> None:
        problem = self.playlist_blocked(pid)
        if problem:
            self._refuse(settled, f"The default playlist was not saved: {problem}", "schedule")
            return
        wanted = "" if pid == runtime_config.FALLBACK_PLAYLIST_ID else pid
        self._settings_write(
            what="The default playlist",
            topics=("schedule",),
            settled=settled,
            after=self._backend.schedule_edited,
            active_playlist=wanted,
        )

    # -- Scheduling: displays ---------------------------------------------------------------
    def assign_display(self, connector: str, pid: str, settled: Settled | None = None) -> None:
        problem = self.playlist_blocked(pid) if pid else ""
        if problem:
            self._refuse(
                settled, f"{connector}\u2019s playlist was not saved: {problem}", "displays"
            )
            return
        store = self._session.displays

        def work() -> object:
            if pid:
                store.assign(connector, pid)
                return None
            return store.unassign(connector)

        self._store_write(
            work,
            what=f"{connector}\u2019s playlist",
            topics=("displays",),
            publish=self._backend.runtime_config_changed,
            settled=settled,
            recheck=(lambda session: runtime_config.use_refusal(pid, session)) if pid else None,
        )

    def set_beats_global_rules(
        self, connector: str, enabled: bool, settled: Settled | None = None
    ) -> None:
        store = self._session.displays
        self._store_write(
            lambda: store.set_beats_global_rules(connector, enabled),
            what=f"{connector}\u2019s priority over the shared rules",
            topics=("displays",),
            publish=self._backend.runtime_config_changed,
            settled=settled,
        )

    def _display_settings(
        self, what: str, changes: dict[str, Any], settled: Settled | None
    ) -> None:
        """Settings → Displays' own rule and save path (`displays_settings`)."""
        attached = [display.connector for display in self.known_displays() if display.connected]
        edit = settings_edit(self._backend.requested_settings, changes, attached)
        if isinstance(edit, str):
            self._refuse(settled, edit, "displays", "settings")
            return
        if not edit:
            self._settle(settled, True)  # saved, or being saved, already
            return
        self._settings_write(
            what=what, topics=("displays", "schedule", "settings"), settled=settled, **edit
        )

    def choose_display_mode(self, mode: str, settled: Settled | None = None) -> None:
        if mode not in config.DISPLAY_MODES:
            self._refuse(settled, f"{mode!r} is not a display mode", "displays")
            return
        self._display_settings("The display mode", {"display_mode": mode}, settled)

    def choose_color_source(self, connector: str, settled: Settled | None = None) -> None:
        self._display_settings(
            "The desktop colors\u2019 display", {"theme_source_connector": connector}, settled
        )

    def forget_plan(self, connector: str) -> ForgetPlan:
        session = self._session
        settings = self._backend.settings
        playlist = dict(session.displays.all()).get(connector, "")
        rules = sum(rule.connector == connector for rule in session.schedules.rules)
        colors = settings.theme_source_connector == connector
        refused = ""
        if any(d.connector == connector and d.connected for d in self.known_displays()):
            refused = f"{connector} is connected; only a display that isn\u2019t can be forgotten"
        elif colors and settings.display_mode == config.DISPLAY_MODE_INDEPENDENT:
            refused = (
                f"Desktop colors are saved to follow {connector}; choose another display "
                "for them first"
            )
        elif not (playlist or rules or colors):
            refused = f"Nothing is kept for {connector}"
        else:
            refused = self.schedule_blocked() or (self.settings_blocked() if colors else "")
        return ForgetPlan(connector, playlist, rules, colors, refused)

    def forget_display(self, connector: str, settled: Settled | None = None) -> None:
        """Forget a detached display: its rules, its own playlist, then the colors' choice.

        One task on the authoring lane, in this order, so that whatever fails
        leaves the least surprising state and is said exactly:

        1. Before anything is written, both stores are read: one that can't
           be read, or is from a newer version, refuses the whole thing.
        2. The schedule: every rule aimed at the display, in one transaction
           (`schedules.Store.remove_for_display`). It is the store with the
           most ways to refuse, so a refusal is most likely here, where
           "nothing changed" is still true.
        3. The display store: its playlist (`displays.Store.forget_display`).
           If this fails after the rules went, the one thing left is that
           playlist, shown on the Displays page, and forgetting again removes
           it. That is reported as such, never as "nothing changed".
        4. The desktop colors' choice, last, and only if it still stands: the
           colors still follow this display and displays are still mirrored,
           where that choice has no effect. Not from when Forget was asked
           for: at the head of the lane against the choices still queued, and
           on the worker against what ``settings.toml`` holds then
           (`ScheduleBackend.prepare_colors_forget`). A newer choice of
           another display for the colors, or of independent displays, wins,
           and Forget never changes the mode.

        Whatever committed is published (`schedule_edited`), through the
        application's own publication and its checks, and the pages show
        what is saved.
        """
        plan = self.forget_plan(connector)
        if plan.refused:
            self._refuse(settled, plan.refused, "displays", "schedule")
            return
        assignments = self._session.displays
        rules = self._session.schedules
        topics = ("displays", "schedule", "settings") if plan.colors else ("displays", "schedule")
        reported = False

        def prepare() -> Callable[[], _Forgotten]:
            # On GTK at the head of the lane: everything queued before this
            # has saved, and what is still queued is newer than this.
            blocked = self.settings_blocked() if plan.colors else ""
            colors = (
                self._backend.prepare_colors_forget(connector)
                if plan.colors and not blocked
                else None
            )

            def work() -> _Forgotten:
                # Never a "Your pick" playlist: those stay (hidden) whatever happens.
                for problem in (rules.durable_problem(), assignments.durable_problem()):
                    if problem:
                        raise ValueError(problem)
                if colors is not None:
                    # Step 4 saves the settings, last. If that save can't be
                    # made, nothing is forgotten (`config.require_color_source_forgettable`).
                    config.require_color_source_forgettable(connector)
                removed = rules.remove_for_display(connector)
                try:
                    playlist = assignments.forget_display(connector)
                except display_store.DisplayError as error:
                    if not removed:
                        raise  # nothing committed: the plain refusal is true
                    return _Forgotten(removed, None, str(error))
                if blocked:
                    return _Forgotten(removed, playlist, colors_error=blocked)
                if colors is None:
                    return _Forgotten(removed, playlist)
                try:
                    return _Forgotten(removed, playlist, colors=colors())
                except Exception as error:
                    return _Forgotten(
                        removed, playlist, colors_error=str(error) or type(error).__name__
                    )

            return work

        def finish(outcome: _Forgotten) -> None:
            committed = bool(outcome.rules or outcome.playlist)
            said, ok = "", True
            if outcome.playlist is None:
                said, ok = self._partly_forgotten(connector, plan, outcome), False
            elif outcome.colors_error:
                said, ok = self._colors_still_follow(connector, outcome), False
            elif outcome.colors is not None and outcome.colors.kept:
                said, ok = self._colors_kept(connector, outcome), committed
            try:
                # The settings first, so what is published is what is saved.
                self._backend.adopt_colors_forget(outcome.colors)
                if committed:
                    self._backend.schedule_edited()  # publish what committed
                self._owner.emit_changed(*topics)  # the pages, before what didn't save is said
                if said:
                    self._backend.window_report(said)
            finally:
                # Settled whatever adopting it did (it reaches the window): the
                # settings come first, so this can't simply move up.
                self._settle(settled, ok)

        def failed(error: str) -> None:
            nonlocal reported
            reported = True
            try:
                self._backend.adopt_colors_forget(None)  # nothing saved: what is requested again
                self._owner.emit_changed(*topics)  # the pages, before the report
                self._backend.window_report(
                    f"Forgetting {connector} was not saved; nothing changed: {error}"
                )
            finally:
                self._settle(settled, False)  # after the settings, whatever they did

        blocked = self.schedule_blocked()
        if blocked:
            self._refuse(settled, blocked, *topics)
            return
        accepted = self._backend.authoring_action_async(
            lambda: _Forgotten((), ""), finish, prepare=prepare, failure=failed
        )
        if not accepted and not reported:
            # Refused without a report (shutting down, or authoring not open yet).
            self._owner.emit_changed(*topics)
            self._settle(settled, False)

    def _colors_still_follow(self, connector: str, outcome: _Forgotten) -> str:
        """The colors' step failed: exactly what went before it."""
        words = _removed_words(outcome, self.playlist_label)
        if not (outcome.rules or outcome.playlist):
            return f"Forgetting {connector} was not saved; nothing changed: {outcome.colors_error}"
        return (
            f"Forgetting {connector} was only partly saved: {runtime_config.listed(words)} "
            f"{'were' if outcome.playlist and outcome.rules else 'was'} removed, but the "
            f"desktop colors still follow it: {outcome.colors_error}"
        )

    def _colors_kept(self, connector: str, outcome: _Forgotten) -> str:
        """A newer choice of independent displays kept the colors on ``connector``."""
        assert outcome.colors is not None
        then = f"Choose another display for them, then forget {connector} again."
        if not (outcome.rules or outcome.playlist):
            return (
                f"{connector} was not forgotten: the desktop colors still follow it, and "
                f"{outcome.colors.kept}. {then}"
            )
        words = runtime_config.listed(_removed_words(outcome, self.playlist_label))
        return (
            f"Forgetting {connector} removed {words}, but the desktop colors still follow "
            f"it: {outcome.colors.kept}. {then}"
        )

    def _partly_forgotten(self, connector: str, plan: ForgetPlan, outcome: _Forgotten) -> str:
        """Exactly what Forget Display removed and what is still saved."""
        count = len(outcome.rules)
        removed = f"its {count} schedule rule{'s were' if count != 1 else ' was'} removed"
        label = self.playlist_label(plan.playlist) or plan.playlist
        left = [f"its own playlist (\u201c{label}\u201d) is still saved"]
        if plan.colors:
            left.append("the desktop colors still follow it")
        return (
            f"Forgetting {connector} was only partly saved: {removed}, but "
            f"{runtime_config.listed(left)}: {outcome.error}"
        )

    # -- Scheduling: runtime commands ----------------------------------------------------------
    def _independent(self, connector: str) -> bool:
        return bool(connector) and self.saved_display_mode() == config.DISPLAY_MODE_INDEPENDENT

    def _runtime_ready(self) -> bool:
        off = self.runtime_blocked()
        if off:
            self._backend.window_report(off)
            return False
        return True

    def set_display_rotation(
        self, connector: str, *, cycle: bool | None = None, shuffle: bool | None = None
    ) -> None:
        try:
            if not self._runtime_ready():
                return
            for verb, value in (("cycle", cycle), ("shuffle", shuffle)):
                if value is None:
                    continue
                argument = "on" if value else "off"
                if self._independent(connector):
                    self._backend.runtime_action_on_async(connector, verb, argument)
                else:
                    self._backend.runtime_action_async(verb, argument)
        finally:
            # The status that follows moves the switches; ask the pages to show
            # the runtime's answer until then, whatever the command did (a
            # refusal is said in the window, which can raise).
            self._owner.emit_changed("routes")

    def use_saved_rotation(self, connector: str) -> None:
        try:
            if not self._runtime_ready():
                return
            if self._independent(connector):
                self._backend.reset_display_modes_on_async(connector)
            else:
                self._backend.runtime_action_async("cycle", "default")
        finally:
            self._owner.emit_changed("routes")  # as above: whatever the command did

    def play_now(self, connector: str, pid: str) -> None:
        try:
            if not self._runtime_ready():
                return
            one = self._independent(connector)
            if not pid:
                if one:
                    self._backend.resume_schedule_on_async(connector)
                else:
                    self._backend.resume_schedule_async()
            else:
                problem = self.playlist_blocked(pid)
                if problem:
                    self._backend.window_report(problem)
                elif one:
                    self._backend.activate_playlist_on_async(connector, pid)
                else:
                    self._backend.activate_playlist_async(pid)
        finally:
            # The row goes back to the runtime's answer whatever the command
            # did: a refusal is said in the window, which can raise.
            self._owner.emit_changed("routes")


def _monitor_displays() -> list[DisplayInfo]:
    """GTK's monitors with a connector name: cheap, and on this thread anyway."""
    display = Gdk.Display.get_default()
    if display is None:
        return []
    found: list[DisplayInfo] = []
    monitors = display.get_monitors()
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        if not isinstance(monitor, Gdk.Monitor):
            continue
        connector = monitor.get_connector()
        if not connector:
            continue
        geometry = monitor.get_geometry()
        scale = getattr(monitor, "get_scale", None)
        found.append(
            DisplayInfo(
                connector=connector,
                model=monitor.get_model() or "",
                connected=True,
                x=geometry.x,
                y=geometry.y,
                width=max(1, geometry.width),
                height=max(1, geometry.height),
                placed=True,
                scale=float(scale()) if callable(scale) else float(monitor.get_scale_factor()),
                refresh=monitor.get_refresh_rate(),
            )
        )
    return found
