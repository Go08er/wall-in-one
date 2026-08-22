"""Offline checks for the GUI's view of one atomic runtime status."""

from wall_in_one.ui import runtime_truth


def test_manual_bar_override_is_read_from_runtime_snapshot() -> None:
    truth = runtime_truth.from_status(
        {
            "playlist_id": "night",
            "playlist": "Night",
            "source": "manual",
            "schedule": {
                "following": False,
                "playlist_id": "day",
                "playlist": "Day",
                "rule_id": "work-hours",
            },
        }
    )

    assert truth is not None
    assert truth.playlist_id == "night"
    assert truth.is_manual
    assert not truth.follows_schedule
    assert truth.scheduled_playlist_id == "day"
    assert truth.schedule_rule_id == "work-hours"
    assert truth.active_playlist_ids == ("night",)


def test_multi_display_snapshot_keeps_every_active_playlist() -> None:
    truth = runtime_truth.from_status(
        {
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "playlists": [
                {"id": "day", "name": "Day", "entries": 2, "active": True},
                {"id": "night", "name": "Night", "entries": 3, "active": True},
                {"id": "spare", "name": "Spare", "entries": 1, "active": False},
            ],
            "schedule": {
                "following": True,
                "playlist_id": "day",
                "playlist": "Day",
                "rule_id": None,
            },
        }
    )

    assert truth is not None
    assert truth.is_multi_display
    assert truth.active_playlist_ids == ("day", "night")
    assert truth.playlist_is_active("day")
    assert truth.playlist_is_active("night")
    assert not truth.playlist_is_active("spare")


def test_incomplete_status_cannot_displace_authoring_fallback() -> None:
    assert runtime_truth.from_status(None) is None
    assert runtime_truth.from_status({"playlist": "Night", "source": "manual"}) is None
    assert (
        runtime_truth.from_status(
            {
                "playlist_id": "",
                "playlist": "Multiple displays",
                "source": "schedule",
                "playlists": [{"id": "day", "active": True}],
            }
        )
        is None
    )
