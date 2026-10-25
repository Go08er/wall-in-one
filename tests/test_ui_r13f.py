"""r13f: the UI's Low and Nit findings from review r13e, as tests.

Statuses are derived from the service's own captured replies
(service/tests/fixtures/stand-in/*.json) in the shapes service/src/runtime.rs writes.
A ``control_`` test is the matching case that was already right.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one.ui import runtime_truth  # noqa: E402
from wall_in_one.ui.status_model import RuntimeStatusModel  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/stand-in"
CANT = "can\u2019t play here"
WEB_OFF = (
    "this configuration doesn't allow web wallpapers: Workshop item 2777242023 is a web "
    "page, so linux-wallpaperengine was not started for it"
)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover
        pytest.skip("no display")
    Adw.init()


def _status(name: str) -> dict[str, Any]:
    status = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(status, dict)
    return status


def _at_the_ceiling(status: dict[str, Any]) -> dict[str, Any]:
    """Steps 1 and 2 of the fitting (runtime.rs abridge_rows :408-419): no ``refused``
    rows, every ``stand_in_for`` gone, and the flag that says so."""
    status = copy.deepcopy(status)
    had = any("stand_in_for" in row for row in status["displays"])
    status["refused_omitted"] = len(status.get("refused", [])) + status.get("refused_omitted", 0)
    status["refused"] = []
    status.pop("stand_in_for", None)
    for row in status["displays"]:
        row.pop("stand_in_for", None)
    if had:
        status["stand_in_for_omitted"] = True
    return status


def _rule_ids_left_out(status: dict[str, Any]) -> dict[str, Any]:
    """Step 6 (abridge_rows :420-425): every row's ``schedule_rule_id`` null. The
    top-level ``schedule.rule_id`` and ``schedules`` stay (runtime.rs:6503-6509)."""
    status = copy.deepcopy(status)
    if any(row.get("schedule_rule_id") for row in status["displays"]):
        status["schedule_rule_id_omitted"] = True
    for row in status["displays"]:
        row["schedule_rule_id"] = None
    return status


def _rule(rule_id: str, playlist: str, *, in_force: bool) -> dict[str, Any]:
    return {
        "id": rule_id,
        "connector": None,
        "playlist_id": playlist,
        "playlist": playlist,
        "months": [],
        "weekdays": [],
        "start": None,
        "end": None,
        "enabled": True,
        "selected": in_force,
        "in_force": in_force,
    }


def _playlists(status: dict[str, Any], active: set[str], extra: tuple[str, ...] = ()) -> None:
    known = {one["id"] for one in status["playlists"]}
    for pid in extra:
        if pid not in known:
            status["playlists"].append({"active": False, "entries": 1, "id": pid, "name": pid})
    for one in status["playlists"]:
        one["active"] = one["id"] in active


def _on_screen(status: dict[str, Any], pid: str) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    model = RuntimeStatusModel()
    model.adopt(status)
    fake = SimpleNamespace(_backend=SimpleNamespace(status_model=model))
    found = RealPlaylists._on_screen(fake, pid)  # type: ignore[arg-type]
    print(f"{pid}: {found!r}")
    return found


def _truth(status: dict[str, Any]) -> runtime_truth.RuntimeTruth:
    truth = runtime_truth.from_status(status)
    assert truth is not None
    for row in truth.displays:
        print(
            f"{row.connector}: route={row.route_source} plays={row.playlist_id} "
            f"stand_in_for={row.stand_in_for} unsure={getattr(row, 'route_unsure', 'n/a')}"
        )
    print(f"summary: plays={truth.playlist_id} stand_in_for={truth.stand_in_for}")
    return truth


def _classic_row(status: dict[str, Any], connector: str) -> str:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(status)
    display = truth.display(connector)
    assert display is not None
    text = SchedulesPage._display_playback_description(display, truth, taboo=False)
    print(f"classic {connector}: {text!r}")
    return text


# ------------------------------------------------------------------ Q1a --------


def _mirrored_assignments(*, ceiling: bool) -> dict[str, Any]:
    """Mirrored, no rule, no pick, a document with [[displays]]: DP-1 -> mixed (plays),
    DP-2 -> pages (a web page, refused; the default ``scenes`` stands in).

    runtime.rs:6283-6312: each row's ``reference`` is its ``assigned_reference``;
    ``route_source`` "assignment"; ``schedule`` the default (no rule selected);
    summary playlist None, so ``playlist_id`` "" / "Multiple displays" (:6159-6168).
    """
    status = copy.deepcopy(_status("pick-refused"))
    status.update(
        source="schedule",
        playlist_id="",
        playlist="Multiple displays",
        entry_id=None,
        kind=None,
        still=None,
        motion_active=True,
    )
    status.pop("stand_in_for", None)
    status["schedule"] = {
        "following": True,
        "playlist": "scenes",
        "playlist_id": "scenes",
        "rule_id": None,
    }
    _playlists(status, {"mixed", "scenes"}, extra=("mixed",))
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    common = dict(
        assignment_source="explicit",
        manual_override=False,
        route_source="assignment",
        schedule_rule_id=None,
    )
    first = {
        **row,
        **common,
        "connector": "DP-1",
        "assigned_playlist": "mixed",
        "assigned_playlist_id": "mixed",
        "playlist": "mixed",
        "playlist_id": "mixed",
        "entry_id": "mixed-1",
    }
    second = {
        **row,
        **common,
        "connector": "DP-2",
        "assigned_playlist": "pages",
        "assigned_playlist_id": "pages",
        "playlist": "scenes",
        "playlist_id": "scenes",
        "entry_id": "scenes-1",
        "stand_in_for": "pages",
    }
    status["displays"] = [first, second]
    return _at_the_ceiling(status) if ceiling else status


def test_q1a_mirrored_a_row_on_its_own_assignment_isnt_a_stand_in() -> None:
    truth = _truth(_mirrored_assignments(ceiling=True))
    first = truth.display("DP-1")
    assert first is not None and first.stand_in_for is None, (
        "DP-1 plays its own assignment 'mixed'; the app says it stands in for "
        f"{first.stand_in_for if first else None!r}"
    )


def test_q1a_mirrored_the_row_that_stands_in_is_known() -> None:
    truth = _truth(_mirrored_assignments(ceiling=True))
    second = truth.display("DP-2")
    assert second is not None and second.stand_in_for == "pages", (
        f"DP-2 asked for 'pages' (assigned_playlist_id) and the app says {second!r}"
    )


def test_q1a_mirrored_playlists_detail_of_the_assignment_isnt_a_stand_in_sentence() -> None:
    text, _playing, _showing = _on_screen(_mirrored_assignments(ceiling=True), "mixed")
    assert CANT not in text, f"'mixed' plays as DP-1's own playlist, and its detail says {text!r}"


def test_q1a_mirrored_classic_display_row_isnt_a_stand_in() -> None:
    text = _classic_row(_mirrored_assignments(ceiling=True), "DP-1")
    assert CANT not in text, text


def test_control_q1a_below_the_ceiling_the_services_word_is_used() -> None:
    truth = _truth(_mirrored_assignments(ceiling=False))
    first, second = truth.display("DP-1"), truth.display("DP-2")
    assert first is not None and second is not None
    assert (first.stand_in_for, second.stand_in_for) == (None, "pages")


def test_control_q1a_independent_the_same_rows_are_inferred_right() -> None:
    status = _mirrored_assignments(ceiling=True)
    status["display_mode"] = "independent"
    truth = _truth(status)
    first, second = truth.display("DP-1"), truth.display("DP-2")
    assert first is not None and second is not None
    assert (first.stand_in_for, second.stand_in_for) == (None, "pages")


# ------------------------------------------------------------------ Q1b --------

RETRY = {"attempt": 1, "maximum_attempts": 3, "reason": "schedule"}


def _mirrored_default_stood_in(*, rule_now: bool, ceiling: bool) -> dict[str, Any]:
    """Mirrored ALL row on the default ``pages`` (a refused web page), the built-in
    ``all-media`` standing in (runtime.rs:6206-6278: route_source "default" with no
    stored rule, ``assigned_playlist_id`` the default, ``stand_in_for`` pages).

    ``rule_now``: rule r1 (-> scenes) is selected now, and its transition is being
    retried, so the stored decision (the row) is still the default while
    ``schedule`` and ``schedules`` say r1 (``automatic_retry`` set on both).
    """
    status = copy.deepcopy(_status("pick-refused"))
    status.update(
        source="schedule",
        playlist_id="all-media",
        playlist="all-media",
        entry_id="all-media-1",
        stand_in_for="pages",
    )
    _playlists(status, {"all-media"})
    [row] = status["displays"]
    row.update(
        assigned_playlist="pages",
        assigned_playlist_id="pages",
        manual_override=False,
        route_source="default",
        schedule_rule_id=None,
        playlist="all-media",
        playlist_id="all-media",
        entry_id="all-media-1",
        stand_in_for="pages",
    )
    if rule_now:
        status["schedule"] = {
            "following": True,
            "playlist": "scenes",
            "playlist_id": "scenes",
            "rule_id": "r1",
        }
        status["schedules"] = [_rule("r1", "scenes", in_force=True)]
        status["automatic_retry"] = dict(RETRY)
        row["automatic_retry"] = dict(RETRY)
        error = (
            "automatic schedule attempt 1/3 failed: renderer exited; previous wallpaper restored"
        )
        status["last_error"] = row["last_error"] = error
    else:
        status["schedule"] = {
            "following": True,
            "playlist": "pages",
            "playlist_id": "pages",
            "rule_id": None,
        }
    return _at_the_ceiling(status) if ceiling else status


def test_q1b_mirrored_default_during_a_retried_transition_names_what_it_asked_for() -> None:
    truth = _truth(_mirrored_default_stood_in(rule_now=True, ceiling=True))
    [row] = truth.displays
    assert row.stand_in_for == "pages", (
        "the row's route is its default 'pages' (assigned_playlist_id), and the app says "
        f"it stands in for {row.stand_in_for!r}"
    )


def test_q1b_mirrored_default_during_a_retried_transition_isnt_said_to_refuse_the_rule() -> None:
    from wall_in_one.ui.next import status_line

    status = _mirrored_default_stood_in(rule_now=True, ceiling=True)
    text, _playing, _showing = _on_screen(status, "all-media")
    model = RuntimeStatusModel()
    model.adopt(status)
    line = status_line.describe(model.view)
    print(f"status line: {line!r}")
    claimed = [said for said in (text, line) if "“scenes”" in said]
    assert not claimed, (
        f"scenes (rule r1's) is being retried, not refused, and the app says {claimed!r}"
    )


def test_control_q1b_mirrored_default_steady_is_inferred() -> None:
    truth = _truth(_mirrored_default_stood_in(rule_now=False, ceiling=True))
    assert truth.displays[0].stand_in_for == "pages"


def test_control_q1b_below_the_ceiling_the_retry_window_reads_right() -> None:
    truth = _truth(_mirrored_default_stood_in(rule_now=True, ceiling=False))
    assert truth.displays[0].stand_in_for == "pages"


# ------------------------------------------------------------------ Q1c --------


def _mirrored_rule_window(*, ceiling: bool) -> dict[str, Any]:
    """Mirrored ALL row on rule r1 (-> pages, refused; the default ``scenes`` stands in).
    r1 ended and r2 (-> web2) is selected; its transition is retried, so the row is
    still r1's decision. At the ceiling the rule ids are left out too (step 6)."""
    status = copy.deepcopy(_status("pick-refused"))
    status.update(source="schedule", stand_in_for="pages")
    _playlists(status, {"scenes"}, extra=("web2",))
    [row] = status["displays"]
    row.update(
        manual_override=False,
        route_source="schedule",
        schedule_rule_id="r1",
        stand_in_for="pages",
        automatic_retry=dict(RETRY),
    )
    status["automatic_retry"] = dict(RETRY)
    status["schedule"] = {
        "following": True,
        "playlist": "web2",
        "playlist_id": "web2",
        "rule_id": "r2",
    }
    status["schedules"] = [
        _rule("r2", "web2", in_force=True),
        _rule("r1", "pages", in_force=False),
    ]
    return _rule_ids_left_out(_at_the_ceiling(status)) if ceiling else status


def test_q1c_mirrored_rule_row_with_ids_left_out_isnt_said_to_refuse_the_new_rule() -> None:
    truth = _truth(_mirrored_rule_window(ceiling=True))
    [row] = truth.displays
    assert row.stand_in_for != "web2", (
        "web2 (rule r2's) is being retried, and the app says it can't play here"
    )


def test_control_q1c_with_rule_ids_the_window_reads_right() -> None:
    status = _at_the_ceiling(_mirrored_rule_window(ceiling=False))
    truth = _truth(status)
    assert truth.displays[0].stand_in_for == "pages"


def test_control_gate_version_1_with_the_flag_infers_nothing() -> None:
    status = _mirrored_default_stood_in(rule_now=False, ceiling=True)
    status["status_version"] = 1
    truth = _truth(status)
    assert all(
        not row.stand_in_for and not getattr(row, "route_unsure", False) for row in truth.displays
    )
    assert truth.stand_in_for is None and not getattr(truth, "route_unsure", False)


def test_control_gate_no_flag_no_inference_on_classic_pages() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    status = _mirrored_default_stood_in(rule_now=False, ceiling=True)
    del status["stand_in_for_omitted"]
    truth = _truth(status)
    assert truth.stand_in_for is None and not truth.displays[0].stand_in_for
    text = SchedulesPage._display_playback_description(truth.displays[0], truth, taboo=False)
    summary = SchedulesPage._playback_description(None, truth)  # type: ignore[arg-type]
    print(f"classic row={text!r} summary={summary!r}")
    assert CANT not in text and CANT not in summary
