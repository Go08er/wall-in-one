"""The new interface's status line says only what the runtime reported."""

from __future__ import annotations

from wall_in_one.ui.next import status_line
from wall_in_one.ui.status_model import RuntimeStatusModel

BATTERY: dict[str, object] = {
    "power_source": "battery",
    "power_available": True,
    "stop_animations_on_battery": True,
    "animations_inhibited": True,
    "animation_inhibition_reason": "battery",
}


def _display(
    connector: str, *, connected: bool = True, state: str = "playing"
) -> dict[str, object]:
    return {
        "connector": connector,
        "connected": connected,
        "assignment_source": "default",
        "assigned_playlist_id": "",
        "assigned_playlist": "",
        "playlist_id": "evening",
        "playlist": "Evening",
        "entry_id": "entry-1",
        "entry_taboo": False,
        "kind": "still",
        "still": "/library/evening.png",
        "motion_active": False,
        "route_source": "schedule",
        "manual_override": False,
        "schedule_rule_id": "rule-1",
        "playback_state": state,
        "paused": state == "paused",
        "stopped": state == "stopped",
        "shuffle": False,
        "shuffle_default": False,
        "shuffle_source": "config",
        "cycle_enabled": True,
        "cycle_default": True,
        "cycle_source": "config",
        "renderer_failed": False,
        "last_error": "",
        "automatic_retry": None,
    }


def two_display_status(
    state: str = "playing",
    *,
    connected: tuple[bool, bool] = (True, True),
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """A schedule-driven version 2 snapshot over DP-1 and HDMI-A-1.

    ``mixed`` is a top-level state only: one display plays, the other pauses.
    """
    states = ("playing", "paused") if state == "mixed" else (state, state)
    return {
        "status_version": 2,
        "display_mode": "independent",
        "theme_source": {"configured": "", "effective": "DP-1", "fallback": False},
        "playlist_id": "evening",
        "playlist": "Evening",
        "source": "schedule",
        "playback_state": state,
        "paused": state == "paused",
        "cycle_enabled": True,
        "shuffle": False,
        "last_error": "",
        "displays": [
            _display("DP-1", connected=connected[0], state=states[0]),
            _display("HDMI-A-1", connected=connected[1], state=states[1]),
        ],
        **(extra or {}),
    }


def test_before_the_first_answer_it_is_checking() -> None:
    assert status_line.describe(RuntimeStatusModel().view) == "Checking the wallpaper service…"


def test_counts_connected_displays_and_names_what_plays() -> None:
    model = RuntimeStatusModel()
    model.adopt(two_display_status())
    assert model.view.truth is not None, "the fixture must be a snapshot the runtime could send"
    assert status_line.describe(model.view) == "2 displays · playing Evening"

    model.adopt(two_display_status(connected=(True, False)))
    assert status_line.describe(model.view) == "1 display · playing Evening"


def test_states_and_power_come_from_the_snapshot() -> None:
    model = RuntimeStatusModel()
    model.adopt(two_display_status("paused", extra=BATTERY))
    assert status_line.describe(model.view) == (
        "2 displays · paused Evening · Animations stopped on battery"
    )
    model.adopt(two_display_status("mixed"))
    assert model.view.truth is not None
    assert status_line.describe(model.view) == "2 displays · partly playing Evening"


def test_a_version_one_snapshot_has_no_display_count() -> None:
    model = RuntimeStatusModel()
    model.adopt({"playlist_id": "all", "playlist": "All media", "source": "manual", "paused": True})
    assert status_line.describe(model.view) == "paused All media"


def test_freshness_marks_follow_the_model() -> None:
    model = RuntimeStatusModel()
    model.adopt(two_display_status())
    model.mark_delayed()
    model.set_busy(True)
    assert status_line.describe(model.view) == (
        "2 displays · playing Evening · status delayed · sending a playback command…"
    )
    model.mark_invalid("Runtime returned a status reply that was not valid JSON")
    assert status_line.describe(model.view).endswith(
        "status delayed · invalid status reply · sending a playback command…"
    )


def test_unavailable_wins_over_a_retained_snapshot() -> None:
    model = RuntimeStatusModel()
    model.adopt(two_display_status())
    model.mark_unavailable(forget=False)
    assert model.status is not None
    assert status_line.describe(model.view) == "Wallpaper service not running"


def test_an_invalid_first_reply_says_so() -> None:
    model = RuntimeStatusModel()
    model.mark_invalid("Runtime rejected its status request: busy")
    assert status_line.describe(model.view) == "Runtime status invalid"
