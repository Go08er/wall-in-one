"""The runtime status model: one holder, every publication delivered.

GTK-free, so these run in the ordinary suite. The classic window's behavior
rests on three properties pinned here: every publication notifies (repeats
included), subscriber failures reach the publisher, and the snapshot is
retained or dropped exactly as `Application.runtime_status` always was.
"""

from __future__ import annotations

import pytest

from wall_in_one.ui.status_model import (
    RuntimeStatusModel,
    RuntimeStatusView,
    StatusChange,
)


def _status(playlist: str = "Evening", **extra: object) -> dict[str, object]:
    return {
        "playlist_id": playlist.casefold(),
        "playlist": playlist,
        "source": "schedule",
        "playback_state": "playing",
        **extra,
    }


def _recording(model: RuntimeStatusModel) -> list[tuple[StatusChange, RuntimeStatusView]]:
    seen: list[tuple[StatusChange, RuntimeStatusView]] = []
    model.subscribe(lambda change, view: seen.append((change, view)))
    return seen


def test_a_new_model_is_checking_with_no_snapshot() -> None:
    view = RuntimeStatusModel().view

    assert view.revision == 0
    assert view.service == "checking"
    assert view.status is None
    assert view.truth is None
    assert not view.delayed
    assert view.protocol_error == ""
    assert not view.busy


def test_every_publication_notifies_even_when_the_content_repeats() -> None:
    model = RuntimeStatusModel()
    seen = _recording(model)
    status = _status()

    model.adopt(status)
    model.adopt(status)
    model.set_busy(True)
    model.set_busy(True)

    assert [change for change, _view in seen] == [
        StatusChange.STATUS,
        StatusChange.STATUS,
        StatusChange.BUSY,
        StatusChange.BUSY,
    ]
    assert [view.revision for _change, view in seen] == [1, 2, 3, 4]
    assert seen[-1][1] is model.view


def test_a_snapshot_clears_delayed_and_invalid_marks() -> None:
    model = RuntimeStatusModel()
    first = _status("Morning")
    model.adopt(first)
    model.mark_delayed()
    model.mark_invalid("Runtime returned a status reply that was not valid JSON")

    view = model.view
    assert view.service == "running"
    assert view.status is first, "a missed deadline or bad reply keeps the last snapshot"
    assert view.delayed
    assert view.protocol_error.startswith("Runtime returned")

    second = _status("Evening")
    model.adopt(second)

    assert model.status is second
    assert not model.view.delayed
    assert model.view.protocol_error == ""


def test_unavailable_forgets_the_snapshot_only_when_asked() -> None:
    model = RuntimeStatusModel()
    status = _status()
    model.adopt(status)
    model.mark_delayed()

    model.mark_unavailable(forget=False)
    assert model.view.service == "unavailable"
    assert model.status is status, "a failed reload has never cleared runtime_status"
    assert not model.view.delayed

    model.mark_unavailable(forget=True)
    assert model.view.service == "unavailable"
    assert model.status is None


def test_busy_is_independent_of_the_snapshot() -> None:
    model = RuntimeStatusModel()
    status = _status()
    model.adopt(status)

    model.set_busy(True)
    assert model.view.busy
    assert model.status is status
    model.mark_unavailable(forget=True)
    assert model.view.busy, "only the command's own completion releases busy"
    model.set_busy(False)
    assert not model.view.busy


def test_the_snapshot_is_parsed_once_per_view() -> None:
    model = RuntimeStatusModel()
    model.adopt(
        _status(
            power_source="battery",
            power_available=True,
            stop_animations_on_battery=True,
            animations_inhibited=True,
            animation_inhibition_reason="battery",
        )
    )
    view = model.view

    truth = view.truth
    assert truth is not None
    assert truth.playlist == "Evening"
    assert view.truth is truth
    assert view.power is not None and view.power.inhibited

    model.adopt({"playlist": "", "source": "nowhere"})
    assert model.view.truth is None, "an unparseable snapshot has no truth"


def test_subscribers_run_in_order_and_failures_reach_the_publisher() -> None:
    model = RuntimeStatusModel()
    order: list[str] = []
    model.subscribe(lambda _change, _view: order.append("first"))

    def broken(_change: StatusChange, _view: RuntimeStatusView) -> None:
        order.append("broken")
        raise RuntimeError("window render failed")

    model.subscribe(broken)
    model.subscribe(lambda _change, _view: order.append("last"))

    with pytest.raises(RuntimeError, match="window render failed"):
        model.adopt(_status())

    assert order == ["first", "broken"]
    assert model.view.service == "running", "the model is updated before anyone is told"


def test_unsubscribe_is_idempotent_and_safe_inside_a_callback() -> None:
    model = RuntimeStatusModel()
    calls: list[str] = []
    unsubscribe_first: list[object] = []

    def first(_change: StatusChange, _view: RuntimeStatusView) -> None:
        calls.append("first")
        stop = unsubscribe_first[0]
        assert callable(stop)
        stop()

    unsubscribe_first.append(model.subscribe(first))
    unsubscribe_second = model.subscribe(lambda _change, _view: calls.append("second"))

    model.set_busy(True)
    model.set_busy(False)
    unsubscribe_second()
    unsubscribe_second()
    model.set_busy(True)

    assert calls == ["first", "second", "second"]
