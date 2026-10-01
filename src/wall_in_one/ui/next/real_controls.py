"""The real `PlaybackControls`: the classic window's runtime verbs, aimed by the bar's scope.

**Where a command goes.** Every control uses the application's own
single-flight runtime lane, the one the classic header, its playback popover
and the Schedules page use:

* "All displays", mirrored displays, or a display that has gone away: the
  global verb (`Application.runtime_action_async`; Resume schedule is
  `Application.resume_schedule_async`).
* One display: the same verb for that display only (``on <connector> <verb>``
  through `Application.runtime_action_on_async`;
  `Application.resume_schedule_on_async`).

Shuffle and "Change wallpaper automatically" are the runtime's live
``shuffle`` and ``cycle`` switches (``on``/``off``) over the saved global
settings, as in the classic popover. Per-playlist overrides are not touched.

**Play.** Its verb is chosen at the press, from the runtime's latest snapshot,
by the rule every window shares (`playback_verbs`): a stopped renderer is
retried, displays that disagree are brought together, otherwise pause or
play. A wallpaper that playback skips (the runtime's session taboo set) is
never sent Play, because the runtime refuses it: the controls open it in the
Library and say why, as the classic window does.

**Nothing optimistic.** No control changes what the bar shows. The
application marks the status model busy while the command is in flight
(`Player.busy`), reports a failure as a toast, and asks the runtime for its
status again; the bar follows that status, as it does after Apply.

**Off.** The controls are off (`Player.controls_off`) until the service has
answered, while it is not running (as the classic popover is), and while a
store saved by a newer version exists: every command first publishes the
runtime configuration, which cannot be compiled then, so each press could
only fail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Final, Protocol

from wall_in_one.ui import playback_verbs, runtime_truth
from wall_in_one.ui.next.state import DisplayView, Player, WallpaperView
from wall_in_one.ui.status_model import RuntimeStatusModel, RuntimeStatusView

#: Said when the application does not start the service (the bar never offers it).
NO_SERVICE_START: Final = (
    "Wall-in-One doesn\u2019t start the wallpaper service. Start it with "
    "\u201csystemctl --user start wall-in-one.service\u201d."
)


class RuntimeBackend(Protocol):
    """What the controls use of `wall_in_one.ui.app.Application`."""

    @property
    def status_model(self) -> RuntimeStatusModel: ...

    def runtime_action_async(self, verb: str, argument: str | None = None) -> bool: ...

    def runtime_action_on_async(
        self, connector: str, verb: str, argument: str | None = None
    ) -> bool: ...

    def resume_schedule_async(self) -> bool: ...

    def resume_schedule_on_async(self, connector: str) -> bool: ...

    def window_report(self, message: str) -> None: ...


class Shown(Protocol):
    """What the controls read of the adapter: the bar's scope and what is on screen."""

    @property
    def scope(self) -> str: ...

    @property
    def display_mode(self) -> str: ...

    @property
    def displays(self) -> Sequence[DisplayView]: ...

    @property
    def current(self) -> Mapping[str, str]: ...

    def has_wallpaper(self, wid: str) -> bool: ...

    def wallpaper(self, wid: str) -> WallpaperView: ...

    def navigate(self, page: str) -> None: ...


class RuntimeControls:
    """`PlaybackControls` over the application's runtime lane, for a `RealAppState`."""

    def __init__(
        self,
        backend: RuntimeBackend,
        shown: Shown,
        files_blocked: Callable[[str], str],
    ) -> None:
        self._backend = backend
        self._shown = shown
        #: Why ``what`` is off because of files from a newer version, or empty.
        self._files_blocked = files_blocked

    # -- what the bar shows ---------------------------------------------------------
    def annotate(self, player: Player, view: RuntimeStatusView) -> Player:
        """``player`` with the transport's state for the bar's scope, from ``view`` alone."""
        off = self.off(view)
        if off:
            return replace(player, busy=view.busy, controls_off=off)
        connector = self._aimed_at(None)
        status = view.status
        truth = view.truth
        assert status is not None and truth is not None  # `off` says why otherwise
        if connector is None:
            retry = status.get("renderer_failed") is True
            skipped = runtime_truth.current_renderer_failure_is_taboo(status)
        else:
            display = truth.display(connector)
            retry = display is not None and display.renderer_failed
            skipped = display is not None and playback_verbs.display_is_taboo(status, display)
        refused = self._refusal(self._skipped(view, connector)) if skipped else ""
        return replace(player, busy=view.busy, retry=retry, play_refused=refused)

    def off(self, view: RuntimeStatusView | None = None) -> str:
        """Why the controls are off now, or empty when they work."""
        view = view or self._backend.status_model.view
        if view.service == "checking":
            return "Checking the wallpaper service\u2026"
        if view.service == "unavailable":
            return "The wallpaper service isn\u2019t running"
        if view.status is None or view.truth is None:
            return "The wallpaper service\u2019s status can\u2019t be read"
        return self._files_blocked("Playback controls")

    # -- PlaybackControls --------------------------------------------------------------
    def toggle_play(self) -> None:
        """Pause, play, retry or bring together, by the classic rule; never Play a skipped one."""
        view = self._ready()
        if view is None:
            return
        status, truth = view.status, view.truth
        assert status is not None and truth is not None
        connector = self._aimed_at(None)
        if connector is None:
            if runtime_truth.current_renderer_failure_is_taboo(status):
                self._lead_to(self._skipped(view, None))
                return
            verb = playback_verbs.play_verb(
                playback_verbs.playback_state(status),
                renderer_failed=status.get("renderer_failed") is True,
            )
            self._backend.runtime_action_async(verb)
            return
        display = truth.display(connector)
        if display is None:
            return
        if playback_verbs.display_is_taboo(status, display):
            self._lead_to(self._skipped(view, connector))
            return
        verb = playback_verbs.play_verb(
            display.playback_state, renderer_failed=display.renderer_failed
        )
        self._backend.runtime_action_on_async(connector, verb)

    def stop(self) -> None:
        self._send(None, "stop")

    def step(self, direction: int, scope: str | None = None) -> None:
        self._send(scope, "next" if direction > 0 else "previous")

    def random(self, scope: str | None = None) -> None:
        self._send(scope, "random")

    def resume_schedule(self, scope: str = "all") -> None:
        if self._ready() is None:
            return
        connector = self._aimed_at(scope)
        if connector is None:
            self._backend.resume_schedule_async()
        else:
            self._backend.resume_schedule_on_async(connector)

    def set_shuffle(self, value: bool, scope: str | None = None) -> None:
        self._send(scope, "shuffle", "on" if value else "off")

    def set_rotate(self, value: bool) -> None:
        self._send(None, "cycle", "on" if value else "off")

    def start_service(self) -> None:
        """The application never starts or stops the service; say how to."""
        self._backend.window_report(NO_SERVICE_START)

    # -- helpers ---------------------------------------------------------------------
    def _ready(self) -> RuntimeStatusView | None:
        """The current view, or None (with the reason reported) while the controls are off."""
        view = self._backend.status_model.view
        off = self.off(view)
        if off:
            self._backend.window_report(off)
            return None
        return view

    def _aimed_at(self, scope: str | None) -> str | None:
        """The one display a command is for, or None for every display."""
        shown = self._shown
        chosen = scope or shown.scope
        if chosen == "all" or shown.display_mode == "mirrored":
            return None
        if chosen not in {display.connector for display in shown.displays}:
            return None
        return chosen

    def _send(self, scope: str | None, verb: str, argument: str | None = None) -> None:
        if self._ready() is None:
            return
        connector = self._aimed_at(scope)
        if connector is None:
            self._backend.runtime_action_async(verb, argument)
        else:
            self._backend.runtime_action_on_async(connector, verb, argument)

    def _skipped(self, view: RuntimeStatusView, connector: str | None) -> str:
        """The id of the skipped wallpaper on ``connector`` (or on any display), or empty."""
        status, truth = view.status, view.truth
        current = self._shown.current
        if truth is not None:
            for display in truth.displays:
                if connector is not None and display.connector != connector:
                    continue
                if display.connected and playback_verbs.display_is_taboo(status, display):
                    return current.get(display.connector, "")
        if connector is not None:
            return current.get(connector, "")
        return next(iter(current.values()), "")

    def _refusal(self, wid: str) -> str:
        """The classic refusal, naming the wallpaper when the library has it."""
        name = self._shown.wallpaper(wid).name if self._shown.has_wallpaper(wid) else ""
        return (
            f"Playback unavailable for {name or 'this wallpaper'}; "
            "see Library for details and removal options"
        )

    def _lead_to(self, wid: str) -> None:
        """Open the skipped wallpaper in the Library and say why Play did nothing."""
        self._shown.navigate(f"library:{wid}" if wid else "library")
        self._backend.window_report(self._refusal(wid))
