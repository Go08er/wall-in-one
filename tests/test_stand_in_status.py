"""What the app reads of a stand-in and of what the service refused (review r13b S1, S2).

The statuses are the service's own, captured by its test
`the_apps_captured_stand_in_statuses_keep_this_services_shape` into
``service/tests/fixtures/stand-in/``: the person used ``playlist-use pages``,
where ``pages`` holds only a web page the service refuses while web
wallpapers are off. The default ``scenes`` plays in its place, so
``playlist_id`` is ``scenes`` and ``stand_in_for`` is ``pages``. The control is
the same pick when the item is a scene, which plays.
"""

from __future__ import annotations

import json
from pathlib import Path

from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.scan import WORKSHOP_PROVIDER
from wall_in_one.ui import runtime_truth
from wall_in_one.ui.next import status_line
from wall_in_one.ui.next.state import Reason, reason_text
from wall_in_one.ui.status_model import RuntimeStatusModel

FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/stand-in"
WEB_PAGE = "2777242023"


def _status(name: str) -> dict[str, object]:
    status = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(status, dict)
    return status


def _view(name: str) -> RuntimeStatusModel:
    model = RuntimeStatusModel()
    model.adopt(_status(name))
    return model


def test_the_next_status_line_says_the_pick_cant_play() -> None:
    # r13b S1: it said "1 display · playing scenes", the stand-in, for a pick of `pages`.
    said = status_line.describe(_view("pick-refused").view)
    assert said == "1 display · playing scenes · “pages” can’t play here", said


def test_control_the_next_status_line_for_a_pick_that_plays() -> None:
    assert status_line.describe(_view("pick-plays").view) == "1 display · playing pages"


def test_runtime_truth_reads_what_a_stand_in_plays_for() -> None:
    truth = runtime_truth.from_status(_status("pick-refused"))
    assert truth is not None and truth.is_manual
    assert truth.playlist_id == "scenes", "what plays stays the stand-in"
    assert truth.stand_in_for == "pages" and truth.stand_in_for_name == "pages"
    assert truth.asked_playlist_id == "pages"
    [row] = truth.displays
    assert row.playlist_id == "scenes" and row.stand_in_for == "pages"
    assert row.stand_in_for_name == "pages" and row.asked_playlist_id == "pages"
    [refused] = truth.refused
    assert refused.scene_id == WEB_PAGE and refused.web_page
    assert "is a web page" in refused.reason
    assert truth.refusal(WEB_PAGE) == refused and truth.refusal("1") is None

    plays = runtime_truth.from_status(_status("pick-plays"))
    assert plays is not None
    assert plays.stand_in_for is None and plays.stand_in_for_name == ""
    assert plays.asked_playlist_id == "pages" == plays.playlist_id
    assert plays.displays[0].stand_in_for is None
    assert plays.refused == () and plays.refused_omitted == 0


def test_the_next_interfaces_reasons_say_the_stand_in() -> None:
    stand_in = "“pages” can’t play here, so “scenes” is playing"
    assert reason_text(Reason("pick", "scenes", stand_in_for="pages")) == (
        f"{stand_in} · your pick"
    )
    assert reason_text(Reason("schedule", "scenes", "18:00", stand_in_for="pages")) == (
        f"{stand_in} · from schedule until 18:00"
    )
    assert reason_text(Reason("display", "scenes", stand_in_for="pages")) == (
        f"{stand_in} · this display’s playlist"
    )
    # A pick of one wallpaper has no name to quote.
    assert reason_text(Reason("pick", "scenes", pick_stood_in=True)) == (
        "Your pick can’t play here, so “scenes” is playing"
    )
    # Controls: as before.
    assert reason_text(Reason("pick", "scenes")) == "scenes · your pick"
    assert reason_text(Reason("pick")) == "Your pick"


def test_the_additive_fields_are_read_leniently() -> None:
    status = _status("pick-refused")
    # A service that predates them says neither; the snapshot still reads.
    older = {
        key: value
        for key, value in status.items()
        if key not in ("stand_in_for", "refused", "refused_omitted")
    }
    older["displays"] = [
        {key: value for key, value in row.items() if key != "stand_in_for"}
        for row in status["displays"]  # type: ignore[attr-defined]
    ]
    truth = runtime_truth.from_status(older)
    assert truth is not None and truth.stand_in_for is None and truth.refused == ()
    # Malformed values are dropped, not the snapshot.
    odd = {
        **status,
        "stand_in_for": 5,
        "refused": [
            {"scene_id": "12", "web_page": False, "reason": "kept"},
            {"scene_id": "../12", "web_page": False, "reason": "not an id"},
            {"scene_id": "13", "web_page": "no", "reason": "not a flag"},
            "not a row",
        ],
        "refused_omitted": -3,
    }
    truth = runtime_truth.from_status(odd)
    assert truth is not None and truth.stand_in_for is None
    assert truth.refused == (runtime_truth.RefusedTruth("12", False, "kept"),)
    assert truth.refused_omitted == 0
    # One naming the playlist that plays is no stand-in.
    same = runtime_truth.from_status({**status, "stand_in_for": "scenes"})
    assert same is not None and same.stand_in_for is None
    assert runtime_truth.refused_from_status({"refused_omitted": 24}) == ((), 24)
    assert runtime_truth.refused_from_status(None) == ((), 0)


def _scene(workshop_id: str, *, base: str = "") -> MediaItem:
    return MediaItem(
        path=Path("/steam/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=0,
        mtime=0,
        provider=WORKSHOP_PROVIDER,
        scene=workshop_id,
        scene_base=base,
        workshop_type="preset" if base else "scene",
    )


def test_refused_items_are_found_by_the_item_their_entries_render() -> None:
    refused = (runtime_truth.RefusedTruth("2910000004", False, "on another Steam library"),)
    other_library = _scene("2910000004")
    preset = _scene("3000000001", base="2910000004")
    plays = _scene("12345")
    still = MediaItem(path=Path("/stills/2910000004"), kind=Kind.STILL, size=0, mtime=0)
    assert runtime_truth.refused_paths(refused, (other_library, preset, plays, still)) == {
        other_library.path: "on another Steam library",
        preset.path: "on another Steam library",
    }
    assert runtime_truth.refused_paths((), (other_library,)) == {}
