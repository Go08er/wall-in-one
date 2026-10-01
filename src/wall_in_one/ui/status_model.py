"""The one place the GUI keeps the runtime status it polls.

`Application` polls the Rust runtime every two seconds and also learns about
it from command replies, reloads and publications. Before this module those
answers went straight into the window through five methods called from about
twenty-five sites, and the last snapshot sat in a private field that the pages
read back through ``Application.runtime_status``.

`RuntimeStatusModel` holds all of it: the last valid snapshot, the service
verdict, the "status delayed" and "invalid reply" marks, and whether a
playback command is in flight. `Application` publishes to it and nothing
else; subscribers are called after every publication.

* The classic window is fed by one forwarding subscriber in `Application`,
  which turns each publication into the `WindowServices` call its call site
  used to make. That keeps `MainWindow`'s rendering exactly as it was.
* The new window subscribes directly and renders from `RuntimeStatusView`.

Rules that keep the classic window's behavior unchanged:

* Every publication notifies, even when the content repeats. The classic
  window re-renders on every poll, and some of its toasts depend on that.
  A subscriber that only wants changes compares ``view.revision`` or fields.
* A subscriber's exception propagates to the publisher. A failing window
  render used to abort the rest of the status adoption; it still does.
* Subscribers must not publish from inside a notification.

Only the runtime status lives here. The authoring gate (migration and
repair) is not pushed to windows today, so it is not modelled yet.

GTK-free and main-thread only: workers hand results to GTK first, as before.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from functools import cached_property
from typing import Literal

from wall_in_one.ui import runtime_truth

#: What the GUI last concluded about the runtime service itself.
#: ``checking`` until the first verdict. ``running`` once a valid snapshot
#: arrived. ``unavailable`` once something proved, or reported, it absent.
ServiceState = Literal["checking", "running", "unavailable"]


class StatusChange(Enum):
    """What one publication was; the forwarding subscriber maps it 1:1."""

    STATUS = "status"
    UNAVAILABLE = "unavailable"
    DELAYED = "delayed"
    INVALID = "invalid"
    BUSY = "busy"


@dataclass(frozen=True, eq=False)
class RuntimeStatusView:
    """One immutable reading of the model, handed to every subscriber.

    ``status`` is the runtime's own JSON object, shared rather than copied as
    it always was; treat it as read-only.
    """

    #: Increases by one with every publication, including repeats.
    revision: int = 0
    service: ServiceState = "checking"
    #: The last valid atomic snapshot, exactly what `Application.runtime_status`
    #: returns. A status probe or runtime command that finds no socket clears
    #: it. Other "unavailable" reports (a failed reload or publication) keep
    #: it, as they always have; check ``service`` before trusting it.
    status: dict[str, object] | None = None
    #: The last status request missed its deadline; ``status`` is stale.
    delayed: bool = False
    #: Why the last status reply was rejected, or empty.
    protocol_error: str = ""
    #: A playback command is in flight on the single runtime lane.
    busy: bool = False

    @cached_property
    def truth(self) -> runtime_truth.RuntimeTruth | None:
        """The snapshot parsed once into `RuntimeTruth`, or None if it does not parse."""
        return runtime_truth.from_status(self.status)

    @cached_property
    def power(self) -> runtime_truth.PowerRuntimeTruth | None:
        """The snapshot's power policy, or None when the runtime reports none."""
        return runtime_truth.power_from_status(self.status)


#: Called after each publication with what happened and the new view.
StatusSubscriber = Callable[[StatusChange, RuntimeStatusView], None]


class RuntimeStatusModel:
    """Holds the latest runtime status and notifies subscribers on every update."""

    def __init__(self) -> None:
        self._view = RuntimeStatusView()
        self._subscribers: list[tuple[object, StatusSubscriber]] = []

    @property
    def view(self) -> RuntimeStatusView:
        """The current reading; a new object after every publication."""
        return self._view

    @property
    def status(self) -> dict[str, object] | None:
        """The last valid snapshot; see `RuntimeStatusView.status`."""
        return self._view.status

    def subscribe(self, subscriber: StatusSubscriber) -> Callable[[], None]:
        """Call ``subscriber`` after every publication; returns its unsubscribe.

        Subscribers run in subscription order. Unsubscribing twice is harmless.
        """
        token = object()
        self._subscribers.append((token, subscriber))

        def unsubscribe() -> None:
            self._subscribers[:] = [entry for entry in self._subscribers if entry[0] is not token]

        return unsubscribe

    # -- publication ------------------------------------------------------

    def adopt(self, status: dict[str, object]) -> None:
        """A valid atomic snapshot arrived; it also clears delayed and invalid."""
        self._publish(
            StatusChange.STATUS,
            replace(
                self._view,
                service="running",
                status=status,
                delayed=False,
                protocol_error="",
            ),
        )

    def mark_unavailable(self, *, forget: bool) -> None:
        """The runtime is not running; ``forget`` also drops the last snapshot."""
        self._publish(
            StatusChange.UNAVAILABLE,
            replace(
                self._view,
                service="unavailable",
                status=None if forget else self._view.status,
                delayed=False,
                protocol_error="",
            ),
        )

    def mark_delayed(self) -> None:
        """One status deadline was missed; the last snapshot stays, marked stale."""
        self._publish(StatusChange.DELAYED, replace(self._view, delayed=True))

    def mark_invalid(self, message: str) -> None:
        """A status reply was malformed or refused; the last snapshot stays."""
        self._publish(StatusChange.INVALID, replace(self._view, protocol_error=message))

    def set_busy(self, busy: bool) -> None:
        """A playback command started (True) or finished (False)."""
        self._publish(StatusChange.BUSY, replace(self._view, busy=busy))

    def _publish(self, change: StatusChange, view: RuntimeStatusView) -> None:
        self._view = view = replace(view, revision=self._view.revision + 1)
        # A snapshot of the list: unsubscribing from inside a callback must
        # not skip the next subscriber.
        for _token, subscriber in tuple(self._subscribers):
            subscriber(change, view)
