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
