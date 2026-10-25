"""r13f's Low and Nits in the UI (REVIEW_R13F_20261025: L3, N4-N9).

Statuses come from ``service/tests/fixtures/stand-in/*.json`` in the shapes
``service/src/runtime.rs`` writes. At the reply ceiling a status is built the way
the service builds it: step 0 leaves out what says nothing (``_lean``), then the
``refused`` rows and every ``stand_in_for`` go (``_at_the_ceiling``), and later the
errors and the rule ids. A mirrored reply with a single row is not one the service
sends at the ceiling (``docs/runtime-config.md``: it leaves more than 600 KB to
spare), so the mirrored shapes here have two rows, a hand-written ``[[displays]]``
document's.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one.ui import runtime_truth, unnamed_pick  # noqa: E402
from wall_in_one.ui.status_model import RuntimeStatusModel, StatusChange  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/stand-in"
CANT = "can\u2019t play here"
WEB_OFF = (
    "this configuration doesn't allow web wallpapers: Workshop item 2777242023 is a web "
    "page, so linux-wallpaperengine was not started for it"
)
TABOO_STALL = 'scheduled playlist "web2" has no usable entries; every entry is taboo this session'


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


def _lean(status: dict[str, Any]) -> dict[str, Any]:
    """Step 0 (runtime.rs LeanSummary::of :477-525, abridge_rows :600-609): every reply
    but the whole one leaves out what says nothing."""
    status = copy.deepcopy(status)
    for key, quiet in (
        ("nothing_to_play", None),
        ("allow_web_wallpapers", False),
        ("refused", []),
        ("refused_omitted", 0),
        ("loaded_overrides_sha256", None),
        ("overrides_ignored", None),
    ):
        if key in status and status[key] == quiet:
            del status[key]
    for row in status["displays"]:
        for key in ("nothing_to_play", "entry_name"):
            if key in row and row[key] is None:
                del row[key]
    return status


def _at_the_ceiling(status: dict[str, Any]) -> dict[str, Any]:
    """Steps 0-2: lean, no ``refused`` rows, every ``stand_in_for`` gone and flagged
    (abridge_rows :571-576; the top level is built from the nulled rows, :6848)."""
    status = copy.deepcopy(status)
    had = any("stand_in_for" in row for row in status["displays"])
    status["refused_omitted"] = len(status.get("refused", [])) + status.get("refused_omitted", 0)
    status["refused"] = []
    status.pop("stand_in_for", None)
    for row in status["displays"]:
        row.pop("stand_in_for", None)
    if had:
        status["stand_in_for_omitted"] = True
    return _lean(status)


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


def _truth(status: dict[str, Any]) -> runtime_truth.RuntimeTruth:
    truth = runtime_truth.from_status(status)
    assert truth is not None
    for row in truth.displays:
        print(
            f"{row.connector}: route={row.route_source} rule={row.schedule_rule_id} "
            f"plays={row.playlist_id} stand_in_for={row.stand_in_for} "
            f"unsure={row.route_unsure} pick_stood_in={getattr(row, 'pick_stood_in', 'n/a')}"
        )
    print(
        f"summary: plays={truth.playlist_id} stand_in_for={truth.stand_in_for} "
        f"unsure={truth.route_unsure} pick_stood_in={getattr(truth, 'pick_stood_in', 'n/a')}"
    )
    return truth


def _routes(status: dict[str, Any]) -> list[Any]:
    from wall_in_one.ui.next.real_schedule import RealScheduling

    routes = RealScheduling._read_routes(None, _truth(status))  # type: ignore[arg-type]
    for route in routes:
        print(
            f"route: {route.route} rule_id={route.rule_id!r} plays={route.playlist_id} "
            f"unsure={route.route_unsure}"
        )
    return list(routes)


def _on_screen(status: dict[str, Any], pid: str) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    model = RuntimeStatusModel()
    model.adopt(status)
    fake = SimpleNamespace(_backend=SimpleNamespace(status_model=model))
    found = RealPlaylists._on_screen(fake, pid)  # type: ignore[arg-type]
    print(f"{pid}: {found!r}")
    return found


# --------------------------------------------------------------------- shapes --


def _mirrored_pick_rows(*, detached: bool = False) -> dict[str, Any]:
    """Mirrored with a hand-written ``[[displays]]`` (the only mirrored document with
    more than one row, the only one near the ceiling: docs/runtime-config.md, step 0's
    paragraph): a pick ``pages`` (refused), ``scenes`` standing in, on every row
    (runtime.rs:6703-6740, ``reference = &self.active_playlist`` with a pick)."""
    status = copy.deepcopy(_status("pick-refused"))
    [row] = status["displays"]
    rows = []
    for connector, connected in (("DP-1", True), ("DP-2", not detached)):
        rows.append(
            {
                **row,
                "connector": connector,
                "connected": connected,
                "assignment_source": "explicit",
                "assigned_playlist": "all-media",
                "assigned_playlist_id": "all-media",
                "motion_active": connected,
            }
        )
    status["displays"] = rows
    return status


def _mirrored_rule_rows(*, on_screen: str, rule: str, stalled: bool) -> dict[str, Any]:
    """Mirrored ``[[displays]]``, two connected rows deciding by ``rule``, playing
    ``on_screen``. ``stalled``: rule r2 (-> web2, every entry taboo) came into force,
    the tick restored r1's baseline with no retry pending (runtime.rs:1806-1812), so
    ``schedules[]`` and ``schedule`` say r2 (fresh, :6585-6600, :7375) while the rows
    keep r1. At the ceiling: errors longer than a rule id go first, then the rule ids
    (step 6, longest first), so the rows' errors are empty and flagged; the top
    level's last_error goes only at step 7."""
    status = copy.deepcopy(_status("pick-plays"))
    status.pop("stand_in_for", None)
    status.update(source="schedule", playlist_id=on_screen, playlist=on_screen)
    known = {one["id"] for one in status["playlists"]}
    for pid in ("pages", "web2"):
        if pid not in known:
            status["playlists"].append({"active": False, "entries": 1, "id": pid, "name": pid})
    for one in status["playlists"]:
        one["active"] = one["id"] == on_screen
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    rows = []
    for connector in ("DP-1", "DP-2"):
        rows.append(
            {
                **row,
                "connector": connector,
                "assignment_source": "explicit",
                "assigned_playlist": "all-media",
                "assigned_playlist_id": "all-media",
                "manual_override": False,
                "route_source": "schedule",
                "schedule_rule_id": None,
                "playlist": on_screen,
                "playlist_id": on_screen,
                "entry_id": f"{on_screen}-1",
                "last_error": "",
                "automatic_retry": None,
            }
        )
    status["displays"] = rows
    now = "r2" if stalled else rule
    status["schedule"] = {
        "following": True,
        "playlist": "web2" if now == "r2" else "pages",
        "playlist_id": "web2" if now == "r2" else "pages",
        "rule_id": now,
    }
    status["schedules"] = [
        _rule("r1", "pages", in_force=now == "r1"),
        _rule("r2", "web2", in_force=now == "r2"),
    ]
    if stalled:
        status["last_error"] = TABOO_STALL
        status["taboo_entries"] = [
            {
                "playlist_id": "web2",
                "playlist": "web2",
                "entry_id": "web2-1",
                "kind": "scene",
                "scene_id": "2910000009",
                "reason": "renderer exited",
                "source": "renderer-crash",
                "durable": False,
                "observed_config_epoch": 1,
            }
        ]
        status["diagnostics_omitted"] = True
    status["schedule_rule_id_omitted"] = True
    return _lean(status)


# ------------------------------------------------------------------ L3 ---------


def _mirrored_pick_plays_nothing(*, nothing: bool) -> dict[str, Any]:
    """Mirrored ALL row: the pick ``pages`` (a refused web page) with nothing that can
    stand in (runtime.rs: the row is pushed with ``nothing_to_play`` and no entry)."""
    status = copy.deepcopy(_status("pick-refused"))
    status.pop("stand_in_for", None)
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    status.update(playlist_id="pages", playlist="pages")
    for one in status["playlists"]:
        one["active"] = one["id"] == "pages"
    row.update(playlist="pages", playlist_id="pages")
    if nothing:
        status.update(nothing_to_play=WEB_OFF, entry_id=None, kind=None, still=None)
        status["motion_active"] = False
        row.update(
            nothing_to_play=WEB_OFF,
            entry_name=f"Nothing to play: {WEB_OFF}",
            entry_id="",
            kind="",
            still="",
            motion_active=False,
        )
    else:
        status["refused"] = []
        row["entry_id"] = status["entry_id"] = "pages-1"
    return status


def _log(statuses: list[dict[str, Any]]) -> list[str]:
    from wall_in_one.ui.activity_log import ActivityLog

    log = ActivityLog(clock=lambda: dt.datetime(2026, 10, 25, 12, 0))
    model = RuntimeStatusModel()
    for status in statuses:
        model.adopt(status)
        log.observe(StatusChange.STATUS, model.view)
    said = [f"{entry.source}: {entry.text}" for entry in log.entries]
    print(f"log={said!r}")
    return said


def test_the_log_doesnt_say_playing_for_a_pick_that_plays_nothing() -> None:
    said = _log([_mirrored_pick_plays_nothing(nothing=True)])
    assert not any("Playing pages" in line for line in said), said


def test_the_log_says_why_nothing_plays_once() -> None:
    nothing = _mirrored_pick_plays_nothing(nothing=True)
    said = _log([nothing, nothing])
    assert [line for line in said if line.startswith("playback:")] == [
        f"playback: Nothing to play: {WEB_OFF}"
    ], said


def test_control_the_log_says_playing_for_a_pick_that_plays() -> None:
    said = _log([_mirrored_pick_plays_nothing(nothing=False)])
    assert "playback: Playing pages (manual)" in said


def _next_status_pill(status: dict[str, Any]) -> tuple[str, str, bool]:
    from wall_in_one.ui.next.displays import DisplaysPage

    [route] = _routes(status)
    fake = SimpleNamespace(
        state=SimpleNamespace(player=lambda: SimpleNamespace(service="running")),
        _route=lambda _connector: route,
    )
    found = DisplaysPage._status(fake, "ALL")  # type: ignore[arg-type]
    print(f"pill={found!r} nothing={route.nothing_to_play!r}")
    return found


def test_next_displays_pill_of_a_display_that_plays_nothing() -> None:
    label, _icon, paused = _next_status_pill(_mirrored_pick_plays_nothing(nothing=True))
    assert label == "Nothing to play" and not paused


def test_next_displays_pill_of_a_display_that_plays_nothing_while_stopped() -> None:
    status = _mirrored_pick_plays_nothing(nothing=True)
    status["displays"][0]["playback_state"] = "stopped"
    # Nothing is the fact; "Still only" would say a still is on screen.
    assert _next_status_pill(status)[0] == "Nothing to play"


def test_control_next_displays_pill_of_a_display_that_plays() -> None:
    assert _next_status_pill(_mirrored_pick_plays_nothing(nothing=False))[0] == "Playing"


def _why_pill(status: dict[str, Any]) -> list[str]:
    from wall_in_one.ui.next.schedule import SchedulePage

    [route] = _routes(status)
    fake = SimpleNamespace(why_entries=[])
    row = SchedulePage._why_unscheduled(fake, route, "ALL")  # type: ignore[arg-type]
    labels: list[str] = []

    def walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                labels.append(child.get_label())
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    return labels


def test_the_why_popover_doesnt_call_a_route_that_plays_nothing_playing() -> None:
    assert "Playing" not in _why_pill(_mirrored_pick_plays_nothing(nothing=True))


def test_control_the_why_popover_calls_a_route_that_plays_playing() -> None:
    assert "Playing" in _why_pill(_mirrored_pick_plays_nothing(nothing=False))


# ------------------------------------------------------------------ N5 ---------


def test_n5_a_stalled_transition_isnt_named_as_the_rows_rule() -> None:
    routes = _routes(_mirrored_rule_rows(on_screen="pages", rule="r1", stalled=True))
    named = [route.rule_id for route in routes]
    assert "r2" not in named, (
        "r2's playlist (web2) is on no display; the rows are r1's decision, and the "
        f"route names {named!r}"
    )


def test_n5_the_schedule_page_doesnt_call_the_stalled_rule_playing() -> None:
    from wall_in_one.ui.next import schedule_model as model
    from wall_in_one.ui.next.schedule_state import RuleView

    [route, _second] = _routes(_mirrored_rule_rows(on_screen="pages", rule="r1", stalled=True))
    winner = route.rule_id if route.route == "schedule" else None
    said = model.reason(
        RuleView(id="r2", playlist="web2"),
        dt.datetime(2026, 10, 25, 12, 0),
        "",
        winner,
        lambda *_a: True,
        mirrored=True,
    )
    print(f"r2 reason={said!r}")
    assert said != ("Playing", "playing"), "pages (r1's) is on screen; r2 is called Playing"


def test_control_n5_a_rule_in_force_on_screen_is_named() -> None:
    routes = _routes(_mirrored_rule_rows(on_screen="pages", rule="r1", stalled=False))
    assert [route.rule_id for route in routes] == ["r1", "r1"]


def test_control_n5_a_retried_transition_names_no_rule() -> None:
    status = _mirrored_rule_rows(on_screen="pages", rule="r1", stalled=True)
    for row in status["displays"]:
        row["automatic_retry"] = {"attempt": 1, "maximum_attempts": 3, "reason": "schedule"}
    assert [route.rule_id for route in _routes(status)] == [None, None]


# ------------------------------------------------------------------ N6 ---------


def _mirrored_rule_one_id_kept() -> dict[str, Any]:
    """Mirrored ``[[displays]]``, rule r1 (-> pages, refused; ``scenes`` stands in) on
    both rows; at the ceiling the service left DP-2's id out only (equal lengths: the
    later row, abridge_rows :585-590)."""
    status = copy.deepcopy(_status("pick-refused"))
    status.update(source="schedule")
    status["schedule"] = {
        "following": True,
        "playlist": "pages",
        "playlist_id": "pages",
        "rule_id": "r1",
    }
    status["schedules"] = [_rule("r1", "pages", in_force=True)]
    [row] = status["displays"]
    rows = []
    for connector, rule in (("DP-1", "r1"), ("DP-2", None)):
        rows.append(
            {
                **row,
                "connector": connector,
                "assignment_source": "explicit",
                "assigned_playlist": "all-media",
                "assigned_playlist_id": "all-media",
                "manual_override": False,
                "route_source": "schedule",
                "schedule_rule_id": rule,
                "stand_in_for": "pages",
            }
        )
    status["displays"] = rows
    status = _at_the_ceiling(status)
    status["schedule_rule_id_omitted"] = True
    return status


def test_n6_a_mirrored_row_whose_sibling_kept_the_id_isnt_hedged() -> None:
    truth = _truth(_mirrored_rule_one_id_kept())
    second = truth.display("DP-2")
    assert second is not None and second.stand_in_for == "pages" and not second.route_unsure, (
        "mirrored rows share one decision and DP-1 says r1; DP-2 is hedged"
    )


def test_n6_the_schedule_page_and_the_inference_agree_on_the_row() -> None:
    [_first, second] = _routes(_mirrored_rule_one_id_kept())
    assert not (second.rule_id is not None and second.route_unsure), (
        f"the Schedule page names {second.rule_id!r} for DP-2 while Displays and Playlists "
        "say they can't tell what it asked for"
    )


def test_control_n6_the_row_that_kept_its_id_is_trusted() -> None:
    truth = _truth(_mirrored_rule_one_id_kept())
    first = truth.display("DP-1")
    assert first is not None and first.stand_in_for == "pages" and not first.route_unsure


def test_a_sibling_that_kept_its_id_is_borrowed_only_when_mirrored() -> None:
    status = _mirrored_rule_one_id_kept()
    status["display_mode"] = "independent"
    first, second = _truth(status).displays
    assert not first.route_unsure and second.route_unsure and second.stand_in_for is None


def test_the_rule_in_force_is_named_when_its_playlist_is_on_screen() -> None:
    # Every row's id left out, r1 (-> pages) in force and pages on screen: r1 it is.
    routes = _routes(_mirrored_rule_rows(on_screen="pages", rule="r1", stalled=False))
    assert [route.rule_id for route in routes] == ["r1", "r1"]


# ------------------------------------------------------------------ N7 ---------


def _independent_pick_at_the_ceiling() -> dict[str, Any]:
    from tests.test_ui_next_r13e import _independent

    status = _at_the_ceiling(_independent("manual", asked="pages", plays="scenes"))
    assert status.get("stand_in_for_omitted") is True
    return status


def test_n7_classic_independent_row_of_an_unsure_pick_hedges() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(_independent_pick_at_the_ceiling())
    [row] = truth.displays
    assert row.route_unsure  # precondition
    text = SchedulesPage._display_playback_description(row, truth, taboo=False)
    print(f"classic row: {text!r}")
    assert "stands in" in text or "stand-in" in text, (
        "the pick may be 'pages' (refused) with 'scenes' standing in; the row says "
        f"{text!r}, i.e. the manual override is scenes"
    )


def test_n7_classic_independent_combo_isnt_put_on_what_may_be_the_stand_in() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(_independent_pick_at_the_ceiling())
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    selected = SchedulesPage._display_playback_selected(truth.displays[0], choices)
    assert selected != 2, "the per-display combo says the pick is scenes"


def test_n7_classic_detail_of_an_unsure_pick_hedges(monkeypatch: pytest.MonkeyPatch) -> None:
    import wall_in_one.ui.playlists_page as page
    from wall_in_one.ui.playlists_page import PlaylistsPage

    monkeypatch.setattr(page, "_playlist_availability", lambda _s, _p: (1, 0))
    monkeypatch.setattr(page, "_is_default", lambda _s, _p: False)
    labels: list[str] = []
    fake = SimpleNamespace(
        _app=SimpleNamespace(runtime_status=_independent_pick_at_the_ceiling()),
        _play_button=SimpleNamespace(
            set_label=labels.append, set_tooltip_text=lambda _t: None, set_sensitive=lambda _s: None
        ),
    )

    class P(list[int]):
        id = "scenes"
        name = "scenes"
        entries = (1,)

    one = P([1])
    detail = PlaylistsPage._playlist_detail(fake, SimpleNamespace(), one)  # type: ignore[arg-type]
    PlaylistsPage._sync_play_button(fake, SimpleNamespace(manual_playlist=None), one)  # type: ignore[arg-type]
    print(f"classic detail={detail!r} button={labels!r}")
    assert labels == ["Playing now · pick or stand-in"]  # precondition: the button hedges
    assert "playing manually" not in detail, (
        f"its own Play button hedges; the detail says {detail!r}"
    )


def test_n7_next_independent_combo_isnt_put_on_what_may_be_the_stand_in() -> None:
    from wall_in_one.ui.next.displays import DisplaysPage

    [route] = _routes(_independent_pick_at_the_ceiling())
    fake = SimpleNamespace(scheduling=SimpleNamespace(manual_pick=lambda: None))
    selected = DisplaysPage._play_selected(fake, route, ["", "pages", "scenes"])  # type: ignore[arg-type]
    assert selected != 2, "the Playing now row says the pick is scenes"


def test_control_n7_next_pages_hedge_the_unsure_pick() -> None:
    text, playing, _showing = _on_screen(_independent_pick_at_the_ceiling(), "scenes")
    assert text == f"On screen now · {runtime_truth.PICK_OR_STAND_IN}" and not playing


def test_n7_the_classic_summary_hedges_an_unsure_pick() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(_independent_pick_at_the_ceiling())
    text = SchedulesPage._playback_description(None, truth)  # type: ignore[arg-type]
    assert runtime_truth.PICK_OR_STAND_IN in text, text


def test_n7_the_classic_overall_combo_isnt_put_on_follow_schedule_or_the_stand_in() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(_independent_pick_at_the_ceiling())
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    session = SimpleNamespace(manual_playlist="scenes")
    assert SchedulesPage._playback_selected(session, choices, truth) is None  # type: ignore[arg-type]


def test_n7_both_status_lines_hedge_an_unsure_pick() -> None:
    from wall_in_one.ui.next import status_line

    model = RuntimeStatusModel()
    model.adopt(_independent_pick_at_the_ceiling())
    assert runtime_truth.PICK_OR_STAND_IN in status_line.describe(model.view)


# ------------------------------------------------------------------ N4 ---------


def _app_with_playlists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    from tests.test_ui_runtime import FakeWindow, _application, _attach
    from wall_in_one.library.model import Kind, Library, MediaItem

    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    items = []
    for name in ("pages", "scenes"):
        path = tmp_path / f"{name}.png"
        items.append(MediaItem(path, Kind.STILL, size=1, mtime=1))
    application.session.adopt_library(Library((tmp_path,), tuple(items)))
    for item, name in zip(items, ("pages", "scenes"), strict=True):
        made = application.session.playlists.create(name, entry_id=name)
        application.session.playlists.add(made.id, item.path)
    return application, window, items


def _send_ok(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    from tests.test_ui_runtime import _status as runtime_status
    from wall_in_one.control import client
    from wall_in_one.control.protocol import Response

    def answered(request: str, argument: str | None = None, **_kwargs: object) -> Response:
        if request == "status":
            return runtime_status("Scheduled")
        sent.append(f"{request} {argument}")
        return Response.success("ok")

    monkeypatch.setattr(client, "send_runtime", answered)


def _classic_overall(session: Any, status: dict[str, Any]) -> int | None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    choices = session.playlists.all()
    selected = SchedulesPage._playback_selected(session, choices, _truth(status))
    names = ["Follow schedule", *(one.name for one in choices)]
    print(f"choices={names} selected={selected!r} record={session.manual_playlist!r}")
    return selected


def _shown_item(row: Adw.ComboRow) -> str:
    item = row.get_selected_item()
    assert isinstance(item, Gtk.StringObject)
    return item.get_string()


def _classic_changed(status: dict[str, Any], picked: int) -> str:
    """The classic Active playlist row after the person picks row ``picked`` and the
    command doesn't start (another GUI runtime call pending: app.py:7405)."""
    from wall_in_one.ui.schedules_page import SchedulesPage

    choices = (
        SimpleNamespace(id="pages", name="pages"),
        SimpleNamespace(id="scenes", name="scenes"),
    )
    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow schedule", "pages", "scenes"]))
    fake = SimpleNamespace(
        _loading=False,
        _app=SimpleNamespace(
            runtime_status=status,
            session=SimpleNamespace(manual_playlist=None),
            activate_playlist_async=lambda _pid: False,
            resume_schedule_async=lambda: False,
        ),
        _playback_selected=SchedulesPage._playback_selected,
    )
    first = SchedulesPage._playback_selected(fake._app.session, choices, _truth(status))
    if first is None:
        unnamed_pick.show(row)
    else:
        row.set_selected(first)
    changed = SchedulesPage._make_playback_changed(fake, choices)  # type: ignore[arg-type]
    row.set_selected(picked)
    changed(row, None)
    shown = _shown_item(row)
    print(f"built on {first!r}; after a command that didn't start: {shown!r}")
    return shown


def test_n4_classic_a_command_that_didnt_start_leaves_no_optimistic_claim() -> None:
    # The pick "pages" holds (stood in for); the person chose "Follow schedule", and
    # nothing was sent. schedules_page.py:1131-1135: "A failure therefore cannot leave
    # an optimistic manual/schedule claim."
    shown = _classic_changed(_at_the_ceiling(_mirrored_pick_rows()), 0)
    assert shown == unnamed_pick.UNNAMED_PICK, f"the row says {shown!r}; the pick still holds"


def test_control_n4_below_the_ceiling_the_row_goes_back_to_the_pick() -> None:
    assert _classic_changed(_mirrored_pick_rows(), 0) == "pages"


def _next_details(status: dict[str, Any], *, shown: int | None = None) -> str:
    """The next Displays page's Playing now row after ``_refresh_details`` builds its
    model (displays.py:528-538) for a mirrored display."""
    from wall_in_one.ui.next.displays import DisplaysPage

    [route, *_rest] = _routes(status)
    row = Adw.ComboRow()
    fake = MagicMock()
    fake.play_row = row
    fake._play_ids = []
    if shown is not None:
        # The row was built before (its model is the page's own) and shows ``shown``.
        fake._play_ids = ["", "pages", "scenes"]
        row.set_model(Gtk.StringList.new(["Follow the schedule", "pages", "scenes"]))
        row.set_selected(shown)
    fake.state.playlists = [
        SimpleNamespace(id="pages", name="pages", automatic=False),
        SimpleNamespace(id="scenes", name="scenes", automatic=False),
    ]
    fake.scheduling.runtime_blocked.return_value = ""
    fake._route.return_value = route
    fake._shown.return_value = None
    fake._status.return_value = ("Playing", "media-playback-start-symbolic", False)
    fake.why.return_value = "why"
    fake._play_selected = lambda r, ids: DisplaysPage._play_selected(fake, r, ids)
    display = SimpleNamespace(connector=route_connector(status), model="", placed=False)
    DisplaysPage._refresh_details(fake, display, True)  # type: ignore[arg-type]
    print(f"next Playing now row: {_shown_item(row)!r} of {fake._play_ids}")
    return _shown_item(row)


def route_connector(status: dict[str, Any]) -> str:
    return str(status["displays"][0]["connector"])


def test_n4_next_a_pick_held_isnt_shown_as_follow_the_schedule() -> None:
    # The page's first build (or any change to the playlists) re-models the row, which
    # starts on row 0; no field names the pick and no record is trusted.
    shown = _next_details(_at_the_ceiling(_mirrored_pick_rows()))
    assert shown == unnamed_pick.UNNAMED_PICK, f"a pick holds, and the row says {shown!r}"


def test_n4_next_a_row_the_person_left_on_a_playlist_stays_there_at_the_ceiling() -> None:
    status = _at_the_ceiling(_mirrored_pick_rows())
    assert _next_details(status, shown=1) == "pages"


def test_n4_next_a_row_on_follow_the_schedule_is_blanked_when_a_pick_holds() -> None:
    status = _at_the_ceiling(_mirrored_pick_rows())
    assert _next_details(status, shown=0) == unnamed_pick.UNNAMED_PICK


def test_control_n4_next_below_the_ceiling_the_row_is_the_pick() -> None:
    assert _next_details(_mirrored_pick_rows()) == "pages"


def test_n4_the_classic_combo_doesnt_rest_on_a_record_the_service_path_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gtk_helpers import spin_until
    from tests.test_ui_runtime import _close
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    application, window, items = _app_with_playlists(tmp_path, monkeypatch)
    sent: list[str] = []
    _send_ok(monkeypatch, sent)
    try:
        # "Your pick" (the quick-choice save's path) records the quick choice ...
        application.session.playlists.set_singleton(
            QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[0].path
        )
        application.session.use_playlist(QUICK_CHOICE_ID)
        # ... then Follow schedule goes through the service, which doesn't clear it.
        assert application.resume_schedule_async()
        spin_until(lambda: window.busy == [True, False])
        assert sent == ["schedule-follow None"]
        assert application.session.manual_playlist == QUICK_CHOICE_ID  # the stale record
        reply = _at_the_ceiling(_mirrored_pick_rows())
        assert _classic_overall(application.session, reply) is None
    finally:
        _close(application)


def test_n4_the_classic_combo_after_the_apps_own_pick_isnt_follow_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gtk_helpers import spin_until
    from tests.test_ui_runtime import _close

    application, window, _items = _app_with_playlists(tmp_path, monkeypatch)
    sent: list[str] = []
    _send_ok(monkeypatch, sent)
    try:
        pages = application.session.playlists.by_name("pages")
        assert application.activate_playlist_async(pages.id)
        spin_until(lambda: window.busy == [True, False])
        assert sent == [f"playlist-use {pages.id}"]
        # The service took the pick; the record is None. The reply can't name the pick.
        assert application.session.manual_playlist is None
        reply = _at_the_ceiling(_mirrored_pick_rows())
        assert _classic_overall(application.session, reply) is None
    finally:
        _close(application)


def _display_row_after(selected: int | None, then: int | None) -> tuple[str, int]:
    from wall_in_one.ui.schedules_page import SchedulesPage

    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow schedule", "pages", "scenes"]))
    fake = SimpleNamespace(_loading=False, _display_selected_echo={})
    for step in (selected, then):
        SchedulesPage._set_display_selected(fake, "DP-1", row, step)  # type: ignore[arg-type]
        # What the handler does with its echo, as GTK's notify would have called it.
        fake._display_selected_echo.clear()
    return _shown_item(row), row.get_model().get_n_items()  # type: ignore[union-attr]


def test_n7_the_classic_display_row_of_an_unsure_pick_names_no_playlist() -> None:
    assert _display_row_after(2, None) == (unnamed_pick.UNNAMED_PICK, 4)


def test_n7_the_classic_display_row_drops_the_item_once_the_pick_is_named() -> None:
    assert _display_row_after(None, 1) == ("pages", 3)


# ------------------------------------------------------------------ N8 ---------


def _independent_renderer_stopped(*, left_out: bool) -> dict[str, Any]:
    """Independent row with a stopped renderer; at the ceiling its error was emptied
    (step 6, per row) and flagged."""
    from tests.test_ui_next_r13e import _independent

    status = _independent("default", asked="scenes", plays="scenes")
    error = "linux-wallpaperengine is not installed: the paired still is shown"
    for record in (status, status["displays"][0]):
        record["renderer_failed"] = True
        record["last_error"] = "" if left_out else error
    if left_out:
        status["diagnostics_omitted"] = True
        status = _lean(status)
    return status


def _classic_row_text(status: dict[str, Any]) -> str:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = _truth(status)
    text = SchedulesPage._display_playback_description(truth.displays[0], truth, taboo=False)
    print(f"classic row: {text!r}")
    return text


def test_n8_classic_row_doesnt_invent_the_error_the_reply_left_out() -> None:
    text = _classic_row_text(_independent_renderer_stopped(left_out=True))
    assert "the motion renderer exited" not in text, (
        f"the reply emptied the error (diagnostics_omitted); the row says {text!r}"
    )


def test_control_n8_classic_row_says_the_error_it_has() -> None:
    assert "not installed" in _classic_row_text(_independent_renderer_stopped(left_out=False))


def _mirrored_renderer_stopped(*, left_out: bool) -> dict[str, Any]:
    status = copy.deepcopy(_status("pick-plays"))
    error = "linux-wallpaperengine exited with status 1"
    status["renderer_failed"] = True
    status["last_error"] = "" if left_out else error
    for row in status["displays"]:
        row["renderer_failed"] = True
        row["last_error"] = "" if left_out else error
    if left_out:
        status["diagnostics_omitted"] = True
        status = _lean(status)
    return status


def test_n8_the_log_doesnt_say_an_error_again_after_a_reply_left_it_out() -> None:
    error = _mirrored_renderer_stopped(left_out=False)
    said = _log([error, _mirrored_renderer_stopped(left_out=True), error])
    renderer = [line for line in said if line.startswith("renderer:")]
    assert len(renderer) == 1, renderer


def test_control_n8_the_log_says_the_same_error_once() -> None:
    error = _mirrored_renderer_stopped(left_out=False)
    said = _log([error, error])
    assert len([line for line in said if line.startswith("renderer:")]) == 1


def test_n8_the_classic_row_says_the_error_was_left_out_not_a_cause() -> None:
    text = _classic_row_text(_independent_renderer_stopped(left_out=True))
    assert runtime_truth.DIAGNOSTICS_LEFT_OUT in text, text


def test_n8_the_log_still_says_a_new_error_after_a_reply_that_left_them_out() -> None:
    first = _mirrored_renderer_stopped(left_out=False)
    other = copy.deepcopy(first)
    other["last_error"] = "linux-wallpaperengine exited with status 2"
    said = _log([first, _mirrored_renderer_stopped(left_out=True), other])
    assert len([line for line in said if line.startswith("renderer:")]) == 2, said


# ------------------------------------------------------------------ N9 ---------


def test_n9_both_uis_say_the_marked_count_before_the_more() -> None:
    from tests.test_ui_next_r13e import _count_line

    line = _count_line(5, marked=True)
    assert line.endswith(f" · 1 {CANT} · and 5 more refused"), line
    assert line == f"3 wallpapers · 1 {CANT} · and 5 more refused"


def test_n9_both_uis_say_a_marked_count_with_nothing_more() -> None:
    from tests.test_ui_next_r13e import _count_line

    assert _count_line(0, marked=True).endswith(f" · 1 {CANT}")


def test_control_n9_with_none_marked_both_say_the_same() -> None:
    from tests.test_ui_next_r13e import _count_line

    assert _count_line(5, marked=False).endswith(f" · 5 {CANT}")


# --------------------------------------------- a delete and the service's pick ----


def _status_naming(pick_id: str, other_id: str) -> dict[str, Any]:
    """pick-refused.json (the pick is refused, another stands in) on these ids."""
    names = {'"pages"': json.dumps(pick_id), '"scenes"': json.dumps(other_id)}
    text = re.sub(
        '"pages"|"scenes"', lambda m: names[m.group(0)], json.dumps(_status("pick-refused"))
    )
    status = json.loads(text)
    assert isinstance(status, dict)
    return status


def _delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pick: str,
    record: str | None = None,
    ceiling: bool = False,
) -> bool:
    """Delete "pages" while the service's pick is ``pick`` (a name): was the pick released?"""
    from tests.test_ui_runtime import _close
    from wall_in_one.ui.app import _PlaylistDeleteResult

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    released: list[bool] = []
    # (Sending it needs a library current with the saved sources, which this fixture
    # isn't; what is judged is whether the delete asks to release the pick.)

    def release() -> bool:
        released.append(True)
        return True

    monkeypatch.setattr(application, "resume_schedule_async", release)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        status = _status_naming(ids[pick], ids["scenes" if pick == "pages" else "pages"])
        if ceiling:
            status = _at_the_ceiling(status)
        application._status_model.adopt(status)
        if record is not None:
            application.session.use_playlist(ids[record])
        # A pick made through the service leaves the app's record as it was.
        failures = application._adopt_playlist_delete(_PlaylistDeleteResult(ids["pages"], "pages"))
        assert failures == ()
        return bool(released)
    finally:
        _close(application)


def test_deleting_the_playlist_the_service_names_as_the_pick_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _delete(tmp_path, monkeypatch, pick="pages")


def test_control_deleting_another_playlist_than_the_pick_keeps_the_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not _delete(tmp_path, monkeypatch, pick="scenes")


def test_control_deleting_the_playlist_the_apps_record_names_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _delete(tmp_path, monkeypatch, pick="scenes", record="pages")


def test_a_pick_the_reply_cant_name_isnt_released_on_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It may be another playlist's pick, which the person wants; the service drops a
    # pick whose playlist the published document no longer has.
    assert not _delete(tmp_path, monkeypatch, pick="pages", ceiling=True)


def _warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pick: str) -> str:
    from tests.test_ui_runtime import _close
    from wall_in_one.ui import playlists_page

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        status = _status_naming(ids[pick], ids["scenes" if pick == "pages" else "pages"])
        truth = runtime_truth.from_status(status)
        return playlists_page._deletion_body(
            application.session, application.session.playlists.get(ids["pages"]), truth
        )
    finally:
        _close(application)


def test_the_classic_delete_warning_names_the_pick_the_service_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "the current manual override" in _warning(tmp_path, monkeypatch, "pages")


def test_control_the_classic_delete_warning_leaves_out_another_playlists_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "manual override" not in _warning(tmp_path, monkeypatch, "scenes")


def test_the_next_delete_warning_names_the_pick_the_service_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        for pick, named in (("pages", True), ("scenes", False)):
            application._status_model.adopt(
                _status_naming(ids[pick], ids["scenes" if pick == "pages" else "pages"])
            )
            view = application._status_model.view
            fake = SimpleNamespace(
                _backend=SimpleNamespace(status_model=application._status_model),
                _session=application.session,
                _editable=lambda _pid: application.session.playlists.get(ids["pages"]),
                _saved_default=lambda: "",
            )
            print(view.service)
            text = RealPlaylists.delete_warning(fake, ids["pages"])  # type: ignore[arg-type]
            assert ("manual override" in text) is named, text
    finally:
        _close(application)
