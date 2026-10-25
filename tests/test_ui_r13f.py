"""r13f: the UI's Low and Nit findings from review r13e, as tests.

Statuses are derived from the service's own captured replies
(service/tests/fixtures/stand-in/*.json) in the shapes service/src/runtime.rs writes.
A ``control_`` test is the matching case that was already right.
"""

from __future__ import annotations

import copy
import datetime as dt
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
WEB_OFF_FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/web-off"
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


# ------------------------------------------------------------------ Q2 ---------


def _mirrored_rule_in_force(*, ids_left_out: bool) -> dict[str, Any]:
    """Mirrored ALL row on rule r1 (-> pages), playing; ``schedules`` says r1 in force."""
    status = copy.deepcopy(_status("pick-plays"))
    status["source"] = "schedule"
    status.pop("stand_in_for", None)
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    row.update(manual_override=False, route_source="schedule", schedule_rule_id="r1")
    status["schedule"] = {
        "following": True,
        "playlist": row["playlist"],
        "playlist_id": row["playlist_id"],
        "rule_id": "r1",
    }
    status["schedules"] = [_rule("r1", row["playlist_id"], in_force=True)]
    return _rule_ids_left_out(status) if ids_left_out else status


def _winner_reason(status: dict[str, Any]) -> tuple[str | None, tuple[str, str]]:
    from wall_in_one.ui.next import schedule_model as model
    from wall_in_one.ui.next.real_schedule import RealScheduling
    from wall_in_one.ui.next.schedule_state import RuleView

    truth = _truth(status)
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    rule = RuleView(id="r1", playlist=route.playlist_id)
    # schedule.py:806-810, mirrored: the winner is the route's rule id.
    winner = route.rule_id if route.route == "schedule" else None
    said = model.reason(
        rule, dt.datetime(2026, 10, 25, 12, 0), "", winner, lambda *_a: True, mirrored=True
    )
    print(f"route.rule_id={route.rule_id!r} reason={said!r}")
    return route.rule_id, said


def test_q2_with_rule_ids_left_out_the_rule_in_force_isnt_overridden() -> None:
    _rule_id, said = _winner_reason(_mirrored_rule_in_force(ids_left_out=True))
    assert said != ("Overridden", "overridden"), (
        "schedules[].in_force says r1 is in force, and the Why popover calls it overridden"
    )


def test_q2_with_rule_ids_left_out_the_route_still_names_the_rule_in_force() -> None:
    rule_id, _said = _winner_reason(_mirrored_rule_in_force(ids_left_out=True))
    assert rule_id == "r1", "no now pill: schedule.py:729 keys on the route's rule id"


def test_control_q2_with_rule_ids_the_rule_is_playing() -> None:
    rule_id, said = _winner_reason(_mirrored_rule_in_force(ids_left_out=False))
    assert (rule_id, said) == ("r1", ("Playing", "playing"))


def test_control_q2_a_retried_transition_names_no_rule_it_cant_be_sure_of() -> None:
    # The row stays on its earlier rule while a transition is retried, so with the id
    # left out the rule now in force is not necessarily the row's.
    status = _mirrored_rule_in_force(ids_left_out=True)
    status["displays"][0]["automatic_retry"] = {
        "attempt": 1,
        "maximum_attempts": 3,
        "reason": "schedule",
    }
    rule_id, _said = _winner_reason(status)
    assert rule_id is None, rule_id


def test_control_q2_independent_names_no_rule_either() -> None:
    # `in_force` is the global answer, not one display's.
    status = _mirrored_rule_in_force(ids_left_out=True)
    status["display_mode"] = "independent"
    rule_id, _said = _winner_reason(status)
    assert rule_id is None, rule_id


# ------------------------------------------------------------------ Q3 ---------


def _mirrored_pick_plays_nothing(*, nothing: bool) -> dict[str, Any]:
    """Mirrored ALL row: the pick ``pages`` (a refused web page) with nothing that can
    stand in (runtime.rs:6206-6278: row pushed with ``nothing_to_play``, no entry,
    ``playlist_id`` the pick; top-level ``nothing_to_play``)."""
    status = copy.deepcopy(_status("pick-refused"))
    status.pop("stand_in_for", None)
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    status.update(playlist_id="pages", playlist="pages")
    _playlists(status, {"pages"})
    row.update(playlist="pages", playlist_id="pages")
    if nothing:
        reason = WEB_OFF
        status.update(nothing_to_play=reason, entry_id=None, kind=None, still=None)
        status["motion_active"] = False
        row.update(
            nothing_to_play=reason,
            entry_name=f"Nothing to play: {reason}",
            entry_id="",
            kind="",
            still="",
            motion_active=False,
        )
    else:
        status["refused"] = []
        row["entry_id"] = status["entry_id"] = "pages-1"
    return status


def test_q3_classic_display_row_of_a_display_that_plays_nothing() -> None:
    text = _classic_row(_mirrored_pick_plays_nothing(nothing=True), "ALL")
    assert "Playing" not in text, f"nothing plays, and the classic row says {text!r}"


def test_control_q3_classic_display_row_that_plays() -> None:
    text = _classic_row(_mirrored_pick_plays_nothing(nothing=False), "ALL")
    assert "Playing" in text, text


def _classic_summary(status: dict[str, Any]) -> str:
    from wall_in_one.ui.schedules_page import SchedulesPage

    text = SchedulesPage._playback_description(None, _truth(status))  # type: ignore[arg-type]
    print(f"classic schedule summary: {text!r}")
    return text


def test_q3_classic_schedule_summary_of_a_pick_that_plays_nothing() -> None:
    text = _classic_summary(_mirrored_pick_plays_nothing(nothing=True))
    assert "is playing" not in text, text


def test_control_q3_classic_schedule_summary_of_a_pick_that_plays() -> None:
    text = _classic_summary(_mirrored_pick_plays_nothing(nothing=False))
    assert text == "Manual override · pages is playing.", text


def _classic_playlists(
    status: dict[str, Any], monkeypatch: pytest.MonkeyPatch, pid: str = "pages"
) -> tuple[str, str]:
    import wall_in_one.ui.playlists_page as page
    from wall_in_one.ui.playlists_page import PlaylistsPage

    monkeypatch.setattr(page, "_playlist_availability", lambda _s, _p: (1, 0))
    monkeypatch.setattr(page, "_is_default", lambda _s, _p: False)
    labels: list[str] = []
    fake = SimpleNamespace(
        _app=SimpleNamespace(runtime_status=status),
        _play_button=SimpleNamespace(
            set_label=labels.append, set_tooltip_text=lambda _t: None, set_sensitive=lambda _s: None
        ),
    )

    class P(list[int]):  # len() and .entries
        id = pid
        name = pid
        entries = (1,)

    one = P([1])
    detail = PlaylistsPage._playlist_detail(fake, SimpleNamespace(), one)  # type: ignore[arg-type]
    PlaylistsPage._sync_play_button(fake, SimpleNamespace(manual_playlist=None), one)  # type: ignore[arg-type]
    print(f"classic detail={detail!r} button={labels!r}")
    return detail, labels[0]


def test_q3_classic_playlists_detail_of_a_pick_that_plays_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail, _button = _classic_playlists(_mirrored_pick_plays_nothing(nothing=True), monkeypatch)
    assert "playing" not in detail, detail


def test_q3_classic_play_button_of_a_pick_that_plays_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _detail, button = _classic_playlists(_mirrored_pick_plays_nothing(nothing=True), monkeypatch)
    assert not button.startswith("Playing now"), button


def test_control_q3_classic_playlists_of_a_pick_that_plays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail, button = _classic_playlists(_mirrored_pick_plays_nothing(nothing=False), monkeypatch)
    assert "playing manually" in detail and button == "Playing now · manual override"


def _classic_window(
    statuses: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, list[str]]:
    from tests.gtk_helpers import destroy_window
    from tests.test_ui_missing_renderer_notice import _classic as classic_application
    from wall_in_one.ui.window import MainWindow

    application = classic_application(tmp_path, monkeypatch)
    window = MainWindow(application, application.settings)  # type: ignore[arg-type, attr-defined]
    reports: list[str] = []
    monkeypatch.setattr(window, "report", reports.append)
    try:
        for status in statuses:
            window.show_runtime_status(status)
        summary, control = window._runtime_summary, window._runtime_status_text
        print(f"classic summary={summary!r} control={control!r} reports={reports!r}")
        return summary, control, reports
    finally:
        destroy_window(window)
        application.session.shutdown()  # type: ignore[attr-defined]


def test_q3_classic_window_summary_of_a_pick_that_plays_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, control, _reports = _classic_window(
        [_mirrored_pick_plays_nothing(nothing=True)], tmp_path, monkeypatch
    )
    assert control.startswith("Nothing to play")  # precondition: the control line knows
    assert "playing pages" not in summary, summary


def test_control_q3_classic_window_summary_of_a_pick_that_plays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, _control, _reports = _classic_window(
        [_mirrored_pick_plays_nothing(nothing=False)], tmp_path, monkeypatch
    )
    assert summary.startswith("playing pages (manual)"), summary


def test_q3_classic_default_row_of_the_captured_web_off_reply() -> None:
    # The service's own reply for an All media library that web-off leaves empty.
    status = json.loads((WEB_OFF_FIXTURES / "status-nothing-to-play.json").read_text("utf-8"))
    text = _classic_row(status, "ALL")
    assert text == "Default rotation · Nothing to play: web wallpapers are off", text


# ------------------------------------------------------------------ Q4 ---------


def _renderer_stopped(*, left_out: bool) -> dict[str, Any]:
    status = copy.deepcopy(_status("pick-plays"))
    error = "linux-wallpaperengine exited with status 1"
    status["renderer_failed"] = True
    status["last_error"] = "" if left_out else error
    for row in status["displays"]:
        row["renderer_failed"] = True
        row["last_error"] = "" if left_out else error
    if left_out:
        status["diagnostics_omitted"] = True
    return status


def test_q4_classic_summary_of_a_stopped_renderer_whose_error_was_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, control, _reports = _classic_window(
        [_renderer_stopped(left_out=True)], tmp_path, monkeypatch
    )
    assert control.startswith("Renderer stopped")  # precondition: renderer_failed is read
    assert "renderer stopped" in summary.casefold(), summary


def test_control_q4_classic_summary_of_a_stopped_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, _control, _reports = _classic_window(
        [_renderer_stopped(left_out=False)], tmp_path, monkeypatch
    )
    assert "renderer stopped" in summary.casefold(), summary


def test_q4_classic_an_error_left_out_isnt_said_again_when_it_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _renderer_stopped(left_out=False)
    _summary, _control, reports = _classic_window(
        [error, _renderer_stopped(left_out=True), error], tmp_path, monkeypatch
    )
    assert len(reports) == 1, reports


def _next_reports(statuses: list[dict[str, Any]]) -> list[str]:
    from wall_in_one.ui.next.window import NextWindow

    reports: list[str] = []
    fake = SimpleNamespace(
        _protocol_error="", _runtime_error="", _missing_said=None, report=reports.append
    )
    for status in statuses:
        NextWindow.show_runtime_status(fake, status)  # type: ignore[arg-type]
    print(f"next reports={reports!r}")
    return reports


def test_q4_next_an_error_left_out_isnt_said_again_when_it_comes_back() -> None:
    error = _renderer_stopped(left_out=False)
    assert len(_next_reports([error, _renderer_stopped(left_out=True), error])) == 1


def test_control_q4_next_the_same_error_twice_is_said_once() -> None:
    error = _renderer_stopped(left_out=False)
    assert len(_next_reports([error, error])) == 1


# ------------------------------------------------------------------ N3 ---------


def _mirrored_pick_stood_in() -> dict[str, Any]:
    """pick-refused.json at the ceiling: the pick ``pages`` is refused, ``scenes`` stands in."""
    status = _at_the_ceiling(_status("pick-refused"))
    assert status["stand_in_for_omitted"] is True and status["display_mode"] == "mirrored"
    return status


def test_q5_mirrored_pick_at_the_ceiling_is_certainly_stood_in_for() -> None:
    text, playing, _showing = _on_screen(_mirrored_pick_stood_in(), "scenes")
    hedge = getattr(runtime_truth, "PICK_OR_STAND_IN", "your pick, or what stands in for it")
    assert hedge not in text, (
        f"mirrored, one row, the flag set: the pick is stood in for, and the detail says {text!r}"
    )
    assert text == "Your pick can\u2019t play here, so \u201cscenes\u201d is playing"
    assert not playing


def test_n3_the_row_and_the_summary_are_a_pick_stood_in_not_unsure() -> None:
    truth = _truth(_mirrored_pick_stood_in())
    [row] = truth.displays
    assert not row.route_unsure and row.stand_in_for is None
    assert not truth.route_unsure
    assert getattr(row, "pick_stood_in", False) and getattr(truth, "pick_stood_in", False)
    # No field names the pick, so nothing is taken for it.
    assert row.asked_playlist_id == "" and truth.asked_playlist_id == ""


def test_n3_the_next_pages_say_it_plainly() -> None:
    from tests.test_ui_next_r13e import _displays_rows
    from wall_in_one.ui.next import status_line
    from wall_in_one.ui.next.real_schedule import RealScheduling

    status = _mirrored_pick_stood_in()
    truth = _truth(status)
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    assert route.route == "pick" and route.quick and route.stand_in_for
    assert not route.route_unsure
    model = RuntimeStatusModel()
    model.adopt(status)
    assert "your pick can\u2019t play here" in status_line.describe(model.view)
    sentence = "Your pick can\u2019t play here, so \u201cscenes\u201d is playing"
    plays, why = _displays_rows(status)
    assert why == sentence, why
    assert plays == f"Not in use now \u00b7 {sentence[0].lower()}{sentence[1:]}", plays


def test_n3_the_classic_pages_say_it_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _mirrored_pick_stood_in()
    sentence = "Your pick can\u2019t play here, so \u201cscenes\u201d is playing"
    assert _classic_summary(status) == f"Manual override \u00b7 {sentence}."
    assert sentence in _classic_row(status, "ALL")
    detail, button = _classic_playlists(status, monkeypatch, "scenes")
    assert "playing manually" not in detail and "pick" in detail, detail
    assert button == "Plays in place of your pick", button


def _classic_selected(status: dict[str, Any], recorded: str | None) -> tuple[Any, Any]:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(status)
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    session = SimpleNamespace(manual_playlist=recorded)
    return (
        SchedulesPage._playback_selected(session, choices, truth),  # type: ignore[arg-type]
        SchedulesPage._display_playback_selected(truth.displays[0], choices),
    )


def test_n3_the_classic_combo_doesnt_rest_on_the_apps_record_of_the_pick() -> None:
    # "scenes" plays in place of the pick. The app's record says "pages", but a pick made
    # through the service (ctl, the companion) never writes it, so it may be stale (r13f N4).
    overall, _display = _classic_selected(_mirrored_pick_stood_in(), "pages")
    assert overall is None, overall


def test_n3_the_classic_combo_stays_when_the_record_doesnt_match() -> None:
    for recorded in (None, "gone"):
        overall, _display = _classic_selected(_mirrored_pick_stood_in(), recorded)
        # Neither "Follow schedule" (0) nor the stand-in "scenes" (2).
        assert overall is None, (recorded, overall)


def test_control_n3_the_classic_combo_below_the_ceiling_is_the_pick() -> None:
    overall, _display = _classic_selected(_status("pick-refused"), None)
    assert overall == 1, overall  # the service names the pick "pages"


def _next_selected(status: dict[str, Any]) -> int | None:
    from wall_in_one.ui.next.displays import DisplaysPage
    from wall_in_one.ui.next.real_schedule import RealScheduling

    truth = _truth(status)
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    fake = SimpleNamespace()
    return DisplaysPage._play_selected(fake, route, ["", "pages", "scenes"])  # type: ignore[arg-type]


def test_n3_the_next_combo_doesnt_rest_on_the_apps_record_of_the_pick() -> None:
    assert _next_selected(_mirrored_pick_stood_in()) is None


def test_control_n3_the_next_combo_below_the_ceiling_is_the_pick() -> None:
    assert _next_selected(_status("pick-refused")) == 1


def test_control_n3_an_independent_pick_at_the_ceiling_is_still_hedged() -> None:
    status = _mirrored_pick_stood_in()
    status["display_mode"] = "independent"
    truth = _truth(status)
    assert truth.displays[0].route_unsure and not getattr(truth.displays[0], "pick_stood_in", False)
    assert truth.route_unsure and not getattr(truth, "pick_stood_in", False)
    text, _playing, _showing = _on_screen(status, "scenes")
    assert text == f"On screen now \u00b7 {runtime_truth.PICK_OR_STAND_IN}", text


def test_control_n3_below_the_ceiling_the_services_word_is_used() -> None:
    truth = _truth(_status("pick-refused"))
    assert truth.displays[0].stand_in_for == "pages"
    assert not getattr(truth.displays[0], "pick_stood_in", False)
    assert not getattr(truth, "pick_stood_in", False)


def test_control_n3_a_mirrored_pick_that_plays_nothing_is_not_a_stand_in() -> None:
    status = _at_the_ceiling(_mirrored_pick_plays_nothing(nothing=True))
    status["stand_in_for_omitted"] = True
    truth = _truth(status)
    assert not getattr(truth.displays[0], "pick_stood_in", False)
    assert not getattr(truth, "pick_stood_in", False)


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


# ------------------------------------------------------------------ N5 ---------


def _classic_count(items: tuple[Any, ...], omitted: int) -> str:
    from tests.test_ui_next_r13e import _classic

    summary, _grid = _classic(items, omitted=omitted)
    return summary


def test_n5_classic_count_keeps_library_items_and_the_services_rows_apart() -> None:
    from tests.test_ui_next_r13e import _scene

    summary = _classic_count((_scene("2910000004"),), omitted=3)
    assert summary.endswith(" · 1 can\u2019t play here \u00b7 and 3 more refused"), summary


def test_control_n5_classic_count_with_no_item_marked_is_the_services_count() -> None:
    from tests.test_ui_next_r13e import _scene

    summary = _classic_count((_scene("12345"),), omitted=3)
    assert summary.endswith(" · 3 can\u2019t play here"), summary


def test_control_n5_classic_count_of_marked_items_alone() -> None:
    from tests.test_ui_next_r13e import _scene

    summary = _classic_count((_scene("2910000004"),), omitted=0)
    assert summary.endswith(" · 1 can\u2019t play here"), summary


def test_n5_both_uis_say_the_unlisted_rows_the_same_way() -> None:
    from tests.test_ui_next_r13e import _count_line, _scene

    said = " \u00b7 1 can\u2019t play here \u00b7 and 3 more refused"
    assert _classic_count((_scene("2910000004"),), omitted=3).endswith(said)
    assert _count_line(3, marked=True).endswith(said)


def test_control_n5_both_uis_say_unlisted_rows_with_no_mark_the_same_way() -> None:
    from tests.test_ui_next_r13e import _count_line, _scene

    said = " \u00b7 3 can\u2019t play here"
    assert _classic_count((_scene("12345"),), omitted=3).endswith(said)
    assert _count_line(3, marked=False).endswith(said)


# ------------------------------------------------- rule ids left out per row ----


def _independent_rules_one_id_kept() -> dict[str, Any]:
    """Independent, two rows on rule r1 (-> pages, refused; ``scenes`` stands in), at the
    ceiling with ``schedule_rule_id_omitted``: the service left out the longer row's id
    only, so DP-1 keeps ``r1`` and DP-2's is null."""
    status = _at_the_ceiling(_mirrored_rule_window(ceiling=False))
    status["display_mode"] = "independent"
    status["automatic_retry"] = None
    [row] = status["displays"]
    row.pop("automatic_retry", None)
    row["automatic_retry"] = None
    first = {**row, "connector": "DP-1", "schedule_rule_id": "r1"}
    second = {**row, "connector": "DP-2", "schedule_rule_id": None}
    status["displays"] = [first, second]
    status["schedule_rule_id_omitted"] = True
    return status


def test_rule_ids_left_out_per_row_only_the_null_row_is_hedged() -> None:
    truth = _truth(_independent_rules_one_id_kept())
    kept, left_out = truth.displays
    assert kept.stand_in_for == "pages" and not kept.route_unsure
    assert left_out.stand_in_for is None and left_out.route_unsure


def test_control_rule_ids_left_out_on_every_row_hedges_every_row() -> None:
    status = _independent_rules_one_id_kept()
    status["displays"][0]["schedule_rule_id"] = None
    truth = _truth(status)
    assert all(row.route_unsure and row.stand_in_for is None for row in truth.displays)


def test_rule_ids_left_out_per_row_the_route_keeps_its_own_id() -> None:
    from wall_in_one.ui.next.real_schedule import RealScheduling

    truth = _truth(_independent_rules_one_id_kept())
    kept, left_out = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    assert kept.rule_id == "r1" and not kept.route_unsure
    assert left_out.rule_id is None and left_out.route_unsure
