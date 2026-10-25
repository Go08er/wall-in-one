"""The UI findings of review r13d, as tests (L1, L3, L4, N2, N5, N6, N10).

Statuses are derived from the captured service replies under
``service/tests/fixtures/stand-in/`` the way ``test_ui_next_stand_in_truth.py``
derives its own. Each ``control_`` test is the matching case that already worked.

L1  The classic " · N can't play here" counts the library's items, not the tiles
    built (the first page of a search's matches).
L3  At the reply ceiling (``refused == []``, ``refused_omitted > 0``, no
    ``stand_in_for``) a default, an assignment and a rule are still known to be
    stood in for, from the row's ``assigned_playlist_id`` and the rule's
    ``schedules[].playlist_id``; a pick can only be hedged.
L4  A display that plays nothing (``nothing_to_play``) is neither "In use now" nor
    "On screen now".
N2  The Playlists detail of a stand-in marks the entry on screen.
N5  Playlists stood in for are quoted one by one.
N6  At the ceiling the count doesn't say "more" with no card marked.
N10 A change to ``refused_omitted`` alone updates the count line only.
"""

from __future__ import annotations

import copy
import dataclasses
import json
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

from wall_in_one.library.model import Kind, MediaItem  # noqa: E402
from wall_in_one.library.scan import WORKSHOP_PROVIDER  # noqa: E402
from wall_in_one.ui import runtime_truth  # noqa: E402
from wall_in_one.ui.status_model import RuntimeStatusModel  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/stand-in"
CANT_PLAY_HERE = "Can\u2019t play here"


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


# ------------------------------------------------------------------ L1 ---------

NOT_FOUND = {
    "scene_id": "2910000004",
    "web_page": False,
    "reason": "Workshop item 2910000004 wasn't found where linux-wallpaperengine looks",
}


def _scene(workshop_id: str) -> MediaItem:
    return MediaItem(
        path=Path("/steam/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=0,
        mtime=0,
        provider=WORKSHOP_PROVIDER,
        scene=workshop_id,
        scene_base="",
        title=f"Item {workshop_id}",
        workshop_type="scene",
        web_page=False,
    )


class _Library:
    def __init__(self, items: tuple[MediaItem, ...]) -> None:
        self.items = items
        self.videos = ()
        self.stills = ()
        self.skipped = ()

    def __len__(self) -> int:
        return len(self.items)


def _classic(
    items: tuple[MediaItem, ...],
    text: str = "",
    refused: list[dict[str, Any]] | None = None,
    omitted: int = 0,
) -> tuple[str, Any]:
    from wall_in_one.library import filter as library_filter
    from wall_in_one.ui.grid import WallpaperGrid
    from wall_in_one.ui.thumbnails import ThumbnailLoader
    from wall_in_one.ui.window import MainWindow

    class Loader(ThumbnailLoader):
        def request(self, item: MediaItem, callback: Any) -> None:
            del item, callback

    grid = WallpaperGrid(Loader(max_workers=1), lambda _item: None)
    grid.populate(items)
    status = {
        **_status("pick-refused"),
        "refused": [NOT_FOUND] if refused is None else refused,
        "refused_omitted": omitted,
    }
    fake = SimpleNamespace(
        _runtime_media_status=status,
        _grid=grid,
        _borked_count=0,
        _summary="",
        _update_subtitle=lambda: None,
        _session_playing_label=MainWindow._session_playing_label,
    )
    session = SimpleNamespace(
        library=_Library(items),
        playlists=SimpleNamespace(all=lambda: (), get=lambda _pid: None),
        active_playlist=lambda: "",
        cursor=None,
    )
    MainWindow._show_media_playback(fake, session)  # type: ignore[arg-type]
    if text:
        # The person searches; the next status poll (or any show_current)
        # recomputes the summary, which _update_subtitle documents as
        # "facts about the library [that] do not change because something is
        # being hidden".
        grid.set_query(library_filter.Query(text=text))
        MainWindow._show_media_playback(fake, session)  # type: ignore[arg-type]
    print(f"summary={fake._summary!r} tiles={len(grid._tiles)}")
    return fake._summary, grid


def _said(summary: str) -> int:
    tail = summary.rsplit(" \u00b7 ", 1)[-1]
    return int(tail.split()[0]) if tail.casefold().endswith(CANT_PLAY_HERE.casefold()) else 0


REFUSED_PATH = Path("/steam/content/431960/2910000004")


def test_l1_a_refused_item_past_the_first_page_is_counted() -> None:
    # 80 others named "Item 10000000xx" sort before "Item 2910000004" by name.
    items = (*tuple(_scene(f"10000000{n:02d}") for n in range(80)), _scene("2910000004"))
    summary, grid = _classic(items)
    assert REFUSED_PATH not in grid._tiles  # precondition: it is past the 72 built
    assert _said(summary) == 1, (
        f"the library holds one item the service refused, and the summary says {summary!r}"
    )


def test_l1_a_search_doesnt_change_the_librarys_count() -> None:
    items = (_scene("2910000004"), _scene("12345"))
    summary, grid = _classic(items, text="12345")
    assert REFUSED_PATH not in grid._tiles  # precondition: the search hides it
    assert _said(summary) == 1, f"a search hid the refused tile, and the summary says {summary!r}"


def test_l1_a_web_page_the_tile_calls_web_off_isnt_counted() -> None:
    web = MediaItem(
        path=Path("/steam/content/431960/2777242023"),
        kind=Kind.SCENE,
        size=0,
        mtime=0,
        provider=WORKSHOP_PROVIDER,
        scene="2777242023",
        scene_base="",
        title="A web page",
        workshop_type="web",
        web_page=True,
    )
    status_refused = {"scene_id": "2777242023", "web_page": True, "reason": "web is off"}
    summary, _grid = _classic((web,), refused=[status_refused])
    assert _said(summary) == 0, summary


def test_l1_a_preset_and_its_base_past_the_first_page_count_once_each() -> None:
    others = tuple(_scene(f"10000000{n:02d}") for n in range(80))
    preset = dataclasses.replace(
        _scene("3000000001"), scene_base="2910000004", workshop_type="preset"
    )
    summary, grid = _classic((*others, _scene("2910000004"), preset))
    assert REFUSED_PATH not in grid._tiles
    assert _said(summary) == 2, summary


def test_control_l1_a_refused_item_on_the_first_page_is_counted() -> None:
    items = (_scene("2910000004"), *tuple(_scene(f"30000000{n:02d}") for n in range(80)))
    summary, grid = _classic(items)
    assert REFUSED_PATH in grid._tiles
    assert _said(summary) == 1, summary


# ------------------------------------------------------------------ N2 ---------


def _on_screen(status: dict[str, Any], pid: str) -> tuple[str, bool, tuple[tuple[str, str], ...]]:
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    model = RuntimeStatusModel()
    model.adopt(status)
    fake = SimpleNamespace(_backend=SimpleNamespace(status_model=model))
    found = RealPlaylists._on_screen(fake, pid)  # type: ignore[arg-type]
    print(f"{pid}: {found!r}")
    return found


def _independent_two(*, second_stands_in_for: str | None) -> dict[str, Any]:
    """pick-refused.json as two independent displays on ``scenes``: DP-1 stands in for
    the pick ``pages``; DP-2 is the pick ``scenes`` itself, or stands in for
    ``second_stands_in_for``."""
    status = copy.deepcopy(_status("pick-refused"))
    status["display_mode"] = "independent"
    status.pop("stand_in_for", None)  # the rows don't agree, or not all stand in
    [row] = status["displays"]
    first = {**row, "connector": "DP-1", "stand_in_for": "pages", "entry_id": "scenes-1"}
    second = {**row, "connector": "DP-2", "entry_id": "scenes-1"}
    second.pop("stand_in_for", None)
    if second_stands_in_for:
        second["stand_in_for"] = second_stands_in_for
    status["displays"] = [first, second]
    if second_stands_in_for:
        status["playlists"].append(
            {
                "active": False,
                "entries": 1,
                "id": second_stands_in_for,
                "name": second_stands_in_for,
            }
        )
    return status


def test_n2_the_stand_in_detail_marks_its_entry_on_screen() -> None:
    # Only the entries: 3433e14 got them right (with UI-A's wrong text); the tip
    # says "\u201cpages\u201d can't play here, so \u201cscenes\u201d is playing" and marks none.
    text, _playing, showing = _on_screen(_status("pick-refused"), "scenes")
    assert showing == (("scenes-1", "ALL"),), (
        f"scenes-1 is on screen and the detail ({text!r}) marks no entry: showing={showing!r}"
    )


def test_n2_a_display_that_stands_in_keeps_its_entry_beside_one_that_picked() -> None:
    _text, _playing, showing = _on_screen(_independent_two(second_stands_in_for=None), "scenes")
    assert set(showing) == {("scenes-1", "DP-1"), ("scenes-1", "DP-2")}, showing


def test_control_n2_a_pick_that_plays_marks_its_entry() -> None:
    _text, _playing, showing = _on_screen(_status("pick-plays"), "pages")
    assert showing, showing


# ------------------------------------------------------------------ L3 ---------


def _independent(
    route_source: str, *, asked: str = "pages", plays: str = "all-media"
) -> dict[str, Any]:
    """An independent display on ``asked`` (by ``route_source``), with ``plays`` on
    screen; ``stand_in_for`` when they differ, as the service writes it."""
    status = copy.deepcopy(_status("pick-refused"))
    status["display_mode"] = "independent"
    status["source"] = "manual" if route_source == "manual" else "schedule"
    status["playlist_id"] = status["playlist"] = plays
    status["entry_id"] = f"{plays}-1"
    status["schedule"] = {
        "following": True,
        "playlist": asked,
        "playlist_id": asked,
        "rule_id": None,
    }
    status.pop("stand_in_for", None)
    for playlist in status["playlists"]:
        playlist["active"] = playlist["id"] == plays
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    row.update(
        connector="DP-1",
        assigned_playlist=asked if route_source in ("default", "assignment") else "scenes",
        assigned_playlist_id=asked if route_source in ("default", "assignment") else "scenes",
        assignment_source="explicit" if route_source == "assignment" else "default",
        manual_override=route_source == "manual",
        route_source=route_source,
        playlist=plays,
        playlist_id=plays,
        entry_id=f"{plays}-1",
        schedule_rule_id=None,
    )
    if route_source == "schedule":
        row["schedule_rule_id"] = "r1"
        status["schedule"]["rule_id"] = "r1"
        status["schedules"] = [
            {
                "id": "r1",
                "connector": None,
                "playlist_id": asked,
                "playlist": asked,
                "months": [],
                "weekdays": [],
                "start": None,
                "end": None,
                "enabled": True,
                "selected": True,
                "in_force": True,
            }
        ]
    if asked != plays:
        status["stand_in_for"] = asked
        row["stand_in_for"] = asked
    return status


def _at_the_ceiling(status: dict[str, Any]) -> dict[str, Any]:
    """What the service sends when the reply would not fit (runtime.rs:5815-5835):
    no ``refused`` rows (each counted in ``refused_omitted``), no ``stand_in_for``."""
    status = copy.deepcopy(status)
    status["refused_omitted"] = len(status.get("refused", [])) + status.get("refused_omitted", 0)
    status["refused"] = []
    status.pop("stand_in_for", None)
    for row in status["displays"]:
        row.pop("stand_in_for", None)
    assert status["refused_omitted"] > 0
    status["stand_in_for_omitted"] = True  # the service says it dropped some
    return status


def _displays_rows(status: dict[str, Any]) -> tuple[str, str]:
    from wall_in_one.ui.next.displays import DisplaysPage
    from wall_in_one.ui.next.real_schedule import RealScheduling

    truth = runtime_truth.from_status(status)
    assert truth is not None
    # The first row's (a mirrored document's rows share one decision).
    [route, *_rest] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    row = Adw.ComboRow()
    fake = SimpleNamespace(
        scheduling=SimpleNamespace(default_playlist="pages", display_assignments=lambda: {}),
        state=SimpleNamespace(
            playlists=[
                SimpleNamespace(id="scenes", name="scenes"),
                SimpleNamespace(id="pages", name="pages"),
            ]
        ),
        _pending_assign={},
        _compact=False,
        _name=lambda pid: pid,
        _plays_ids=[],
        plays_row=row,
        _route=lambda _connector: route,
    )
    fake.why = lambda connector: DisplaysPage.why(fake, connector)  # type: ignore[arg-type]
    DisplaysPage._refresh_plays(fake, "DP-1", "", route)  # type: ignore[arg-type]
    why = DisplaysPage.why(fake, "DP-1")  # type: ignore[arg-type]
    plays = row.get_subtitle() or ""
    print(f"plays={plays!r} why={why!r}")
    return plays, why


@pytest.mark.parametrize("source", ["default", "assignment"])
def test_l3_at_the_ceiling_a_refused_default_or_assignment_isnt_in_use(source: str) -> None:
    status = _at_the_ceiling(_independent(source))
    row = status["displays"][0]
    # What the row still says, with every stand_in_for gone: it asked for pages.
    assert row["assigned_playlist_id"] == "pages" and row["playlist_id"] == "all-media"
    plays, _why = _displays_rows(status)
    assert plays != "In use now", (
        f"'{source}' asked for pages, all-media plays, and the row says {plays!r}"
    )


def test_l3_at_the_ceiling_a_rule_isnt_said_to_schedule_its_stand_in() -> None:
    status = _at_the_ceiling(_independent("schedule"))
    assert status["schedules"][0]["playlist_id"] == "pages"
    plays, _why = _displays_rows(status)
    assert "\u201call-media\u201d is scheduled" not in plays, (
        f"rule r1 schedules pages, and the row says {plays!r}"
    )


def test_l3_at_the_ceiling_the_stand_in_isnt_presented_as_the_pick() -> None:
    # Only a hedge is possible here (status has no pick field), and it is: the
    # reply says something is refused (refused_omitted > 0) and lists none of it.
    status = _at_the_ceiling(_status("pick-refused"))
    text, playing, _showing = _on_screen(status, "scenes")
    assert not ("your pick" in text and playing), (
        f"refused == [] with refused_omitted == {status['refused_omitted']}, "
        f"and the stand-in is {text!r} with playing={playing}"
    )


@pytest.mark.parametrize(
    ("source", "words"),
    [("assignment", "this display\u2019s playlist"), ("schedule", "from schedule")],
)
def test_l3_at_the_ceiling_the_playlists_detail_doesnt_give_the_stand_in_the_asked_reason(
    source: str, words: str
) -> None:
    # The default ``scenes`` stands in for ``pages`` (the assignment, or rule r1's).
    status = _at_the_ceiling(_independent(source, plays="scenes"))
    text, _playing, _showing = _on_screen(status, "scenes")
    assert words not in text, (
        f"the row still names pages as what {source} asked for, and scenes says {text!r}"
    )


@pytest.mark.parametrize(
    ("source", "words"),
    [("assignment", "this display\u2019s playlist"), ("schedule", "from schedule")],
)
def test_control_l3_the_playlists_detail_of_what_was_asked_and_plays(
    source: str, words: str
) -> None:
    text, _playing, _showing = _on_screen(_independent(source, plays="pages"), "pages")
    assert text == f"On screen now \u00b7 {words}", text


@pytest.mark.parametrize("source", ["assignment", "schedule"])
def test_control_l3_below_the_ceiling_the_playlists_detail_says_the_stand_in(source: str) -> None:
    text, _playing, _showing = _on_screen(_independent(source, plays="scenes"), "scenes")
    assert text == runtime_truth.stand_in_sentence("pages", "scenes"), text


@pytest.mark.parametrize("source", ["default", "assignment"])
def test_control_l3_a_default_or_assignment_that_plays_is_in_use(source: str) -> None:
    plays, _why = _displays_rows(_independent(source, plays="pages"))
    assert plays == "In use now", plays


def test_control_l3_a_rule_that_plays_is_scheduled() -> None:
    plays, _why = _displays_rows(_independent("schedule", plays="pages"))
    assert plays == "Not in use now \u00b7 \u201cpages\u201d is scheduled", plays


@pytest.mark.parametrize("source", ["default", "assignment", "schedule"])
def test_control_l3_below_the_ceiling_it_says_so(source: str) -> None:
    plays, _why = _displays_rows(_independent(source))
    assert "can\u2019t play here" in plays.casefold() or CANT_PLAY_HERE in plays, plays


def test_control_l3_a_pick_that_plays_is_the_pick() -> None:
    status = _status("pick-plays")
    status["refused"], status["refused_omitted"] = [], 0
    assert _on_screen(status, "pages")[:2] == ("On screen now \u00b7 your pick", True)


# ------------------------------------------------------------------ L4 ---------


def _nothing_to_play(*, nothing: bool) -> dict[str, Any]:
    """An independent display on its default ``pages``, every entry refused and
    nothing that can stand in: the row names ``pages``, plays nothing, says why,
    and carries no ``stand_in_for`` (runtime.rs:6488-6540, docs/runtime-config.md
    "When nothing can play at all ... with no stand_in_for")."""
    status = _independent("default", plays="pages")
    if nothing:
        reason = "this configuration doesn't allow web wallpapers"
        status["nothing_to_play"] = reason
        status["entry_id"] = None
        status["kind"] = None
        status["still"] = None
        row = status["displays"][0]
        row.update(
            nothing_to_play=reason,
            entry_name=f"Nothing to play: {reason}",
            entry_id="",
            kind="",
            still="",
            motion_active=False,
        )
    return status


def test_l4_a_display_that_plays_nothing_isnt_in_use_now() -> None:
    plays, why = _displays_rows(_nothing_to_play(nothing=True))
    assert plays != "In use now", f"nothing plays on DP-1, and its default is {plays!r} ({why!r})"


def test_l4_the_playlist_of_a_display_that_plays_nothing_isnt_on_screen() -> None:
    text, _playing, _showing = _on_screen(_nothing_to_play(nothing=True), "pages")
    assert not text.startswith("On "), f"nothing plays, and pages says {text!r}"


def test_control_l4_a_default_that_plays_is_in_use_and_on_screen() -> None:
    status = _nothing_to_play(nothing=False)
    assert _displays_rows(status)[0] == "In use now"
    assert _on_screen(status, "pages")[0] == "On screen now \u00b7 nothing scheduled"


# ------------------------------------------------------------------ N5 ---------


def test_n5_two_playlists_stood_in_for_are_two_names() -> None:
    text, _playing, _showing = _on_screen(_independent_two(second_stands_in_for="web2"), "scenes")
    assert "\u201cpages and web2\u201d" not in text, text


def test_control_n5_one_playlist_stood_in_for_is_one_name() -> None:
    status = _independent_two(second_stands_in_for="pages")
    text, _playing, _showing = _on_screen(status, "scenes")
    assert text == runtime_truth.stand_in_sentence("pages", "scenes"), text


def test_control_n5_the_two_names_are_each_quoted() -> None:
    text, _playing, _showing = _on_screen(_independent_two(second_stands_in_for="web2"), "scenes")
    assert text.startswith("\u201cpages\u201d and \u201cweb2\u201d can\u2019t play here"), text


# ------------------------------------------------------------------ L3 more ----


def test_l3_the_row_still_says_what_it_asked_for_at_the_ceiling() -> None:
    # What every page keys on: the truth, not the page, works the stand-in out.
    for source in ("default", "assignment", "schedule"):
        truth = runtime_truth.from_status(_at_the_ceiling(_independent(source)))
        assert truth is not None
        [row] = truth.displays
        assert (row.stand_in_for, row.stand_in_for_name) == ("pages", "pages"), source
        assert truth.stand_in_for == "pages", source
        assert not row.route_unsure and not truth.route_unsure


def test_l3_a_mirrored_default_at_the_ceiling_is_known_too() -> None:
    status = _at_the_ceiling(copy.deepcopy(_status("pick-refused")))
    status["source"] = "schedule"
    status["schedule"] = {
        "following": True,
        "playlist": "pages",
        "playlist_id": "pages",
        "rule_id": None,
    }
    # The row's own decision is the default "pages" (what the service writes as its
    # assignment), whatever the schedule chooses now.
    status["displays"][0].update(
        route_source="default",
        manual_override=False,
        assigned_playlist="pages",
        assigned_playlist_id="pages",
    )
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.displays[0].stand_in_for == "pages"


def test_l3_an_independent_pick_at_the_ceiling_is_unsure_not_claimed() -> None:
    truth = runtime_truth.from_status(_at_the_ceiling(_independent("manual")))
    assert truth is not None
    assert truth.route_unsure and truth.displays[0].route_unsure
    assert truth.stand_in_for is None and not truth.pick_stood_in


def test_l3_the_pick_hedge_names_what_plays_and_doesnt_claim_the_pick() -> None:
    from wall_in_one.ui.next.state import Reason, reason_text

    text, playing, _showing = _on_screen(_at_the_ceiling(_independent("manual")), "all-media")
    assert text == f"On screen now \u00b7 {runtime_truth.PICK_OR_STAND_IN}" and not playing
    said = reason_text(Reason("pick", "scenes", route_unsure=True))
    assert said == "\u201cscenes\u201d is playing", said
    assert "pick" not in reason_text(Reason("pick", "scenes", route_unsure=True)).casefold()


def test_l3_nothing_is_inferred_below_the_ceiling() -> None:
    # stand_in_for is the service's word wherever it is sent; a row without it
    # plays its own, even if the pick's route names another playlist.
    status = _independent("assignment", plays="pages")
    status["refused_omitted"] = 1  # the ceiling's omitted count, but refused is not empty
    status["refused"] = [NOT_FOUND]
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.displays[0].stand_in_for is None
    assert not truth.displays[0].route_unsure


def test_control_l3_a_row_that_plays_what_it_asked_for_is_not_a_stand_in() -> None:
    truth = runtime_truth.from_status(_at_the_ceiling(_independent("assignment", plays="pages")))
    assert truth is not None and truth.displays[0].stand_in_for is None


def test_l3_the_classic_play_button_doesnt_claim_the_pick_at_the_ceiling() -> None:
    from wall_in_one.ui.playlists_page import PlaylistsPage

    truth = runtime_truth.from_status(_at_the_ceiling(_status("pick-refused")))
    assert truth is not None
    sync = PlaylistsPage._sync_play_button
    labels: list[str] = []
    fake = SimpleNamespace(
        _app=SimpleNamespace(runtime_status=_at_the_ceiling(_status("pick-refused"))),
        _play_button=SimpleNamespace(
            set_label=labels.append, set_tooltip_text=lambda _t: None, set_sensitive=lambda _s: None
        ),
    )
    playlist = SimpleNamespace(id="scenes", name="scenes", entries=(1,))
    import wall_in_one.ui.playlists_page as page

    original = page._playlist_availability
    page._playlist_availability = lambda _s, _p: (True, 0)  # type: ignore[assignment]
    try:
        sync(fake, SimpleNamespace(manual_playlist=None), playlist)  # type: ignore[arg-type]
    finally:
        page._playlist_availability = original
    assert labels and "manual override" not in labels[0], labels


# ------------------------------------------------------------------ L4 more ----


def test_l4_the_row_carries_the_reason_and_the_pages_say_it() -> None:
    status = _nothing_to_play(nothing=True)
    reason = "this configuration doesn't allow web wallpapers"
    plays, why = _displays_rows(status)
    assert plays == f"Nothing to play: {reason}", plays
    assert why == f"Nothing to play: {reason}", why
    text, playing, showing = _on_screen(status, "pages")
    assert (text, playing, showing) == (f"Nothing to play: {reason}", False, ())


def test_l4_a_playlist_one_display_plays_and_another_doesnt() -> None:
    status = _nothing_to_play(nothing=False)
    second = {**status["displays"][0], "connector": "DP-2"}
    second.update(
        nothing_to_play="no entry can play",
        entry_name="Nothing to play: no entry can play",
        entry_id="",
    )
    status["displays"] = [status["displays"][0], second]
    text, _playing, showing = _on_screen(status, "pages")
    assert text.startswith("On DP-1 now") and text.endswith("nothing to play on DP-2"), text
    assert showing == (("pages-1", "DP-1"),), showing


# ------------------------------------------------------------------ N6 ---------


def _count_line(omitted: int, *, marked: bool) -> str:
    from wall_in_one.ui.next.library import LibraryPage

    state = SimpleNamespace(
        wallpapers=tuple(
            SimpleNamespace(refused_here="not found" if marked and n == 0 else "") for n in range(3)
        ),
        library_skipped=(),
        library_scanning=False,
        library_refused_omitted=omitted,
    )
    fake = MagicMock()
    fake.state = state
    fake._count = Gtk.Label()
    fake._matching = 3
    fake._cards = {}
    LibraryPage._update_count(fake)
    return str(fake._count.get_label())


def test_n6_with_no_card_marked_the_count_isnt_more() -> None:
    assert _count_line(5, marked=False) == "3 wallpapers \u00b7 5 can\u2019t play here"


def test_control_n6_with_cards_marked_the_rest_are_more() -> None:
    assert _count_line(5, marked=True) == (
        "3 wallpapers \u00b7 1 can\u2019t play here \u00b7 and 5 more refused"
    )


def test_n6_the_inspector_doesnt_say_the_mark_waits_for_the_refusal_to_end() -> None:
    from wall_in_one.ui.next.inspector import Inspector

    card = Inspector._refused_here(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(refused_here="not found"),
    )
    texts: list[str] = []

    def walk(widget: Any) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                texts.append(child.get_label())
            walk(child)
            child = child.get_next_sibling()

    walk(card)
    assert any("stops naming it" in text for text in texts), texts


# ------------------------------------------------------------------ N10 --------


def _adopt(status: dict[str, Any], before_omitted: int = 0) -> str:
    from wall_in_one.ui.next.real_state import RealAppState

    truth = runtime_truth.from_status(status)
    assert truth is not None
    fake = SimpleNamespace(_refused={}, _refused_omitted=before_omitted, _wallpapers=(), _index={})
    fake._refused_reason = lambda _item: ""
    return RealAppState._adopt_refused(fake, truth)  # type: ignore[arg-type]


def test_n10_only_the_omitted_count_moving_is_not_a_library_rebuild() -> None:
    status = _at_the_ceiling(_status("pick-refused"))
    assert _adopt(status, before_omitted=status["refused_omitted"] - 1) == "library-count"


def test_control_n10_a_card_that_changes_rebuilds_the_library() -> None:
    from dataclasses import dataclass

    from wall_in_one.ui.next.real_state import RealAppState

    @dataclass(frozen=True)
    class View:
        item: MediaItem
        id: str = "w1"
        problem: bool = False
        refused_here: str = ""

    status = {**_status("pick-refused"), "refused": [NOT_FOUND], "refused_omitted": 0}
    truth = runtime_truth.from_status(status)
    assert truth is not None
    fake = SimpleNamespace(
        _refused={}, _refused_omitted=0, _wallpapers=(View(_scene("2910000004")),), _index={}
    )
    fake._refused_reason = lambda _item: "not found"
    assert RealAppState._adopt_refused(fake, truth) == "library"  # type: ignore[arg-type]


def test_n10_the_library_page_follows_the_count_topic_without_a_rebuild() -> None:
    from wall_in_one.ui.next.library import LibraryPage

    fake = MagicMock()
    LibraryPage._on_changed(fake, MagicMock(), "library-count")
    fake._update_count.assert_called_once_with()
    fake._rebuild.assert_not_called()


def test_control_n10_the_library_topic_rebuilds() -> None:
    from wall_in_one.ui.next.library import LibraryPage

    fake = MagicMock()
    LibraryPage._on_changed(fake, MagicMock(), "library")
    fake._rebuild.assert_called_once_with()


# ------------------------------------------------------------------ L3 trigger --


def test_l3_no_flag_no_inference_even_in_the_old_heuristics_shape() -> None:
    # refused == [] with refused_omitted > 0 is no longer taken for a dropped
    # stand_in_for; an older service never sends the flag.
    for source in ("default", "assignment", "schedule", "manual"):
        base = _independent(source) if source != "manual" else _status("pick-refused")
        status = _at_the_ceiling(base)
        del status["stand_in_for_omitted"]
        truth = runtime_truth.from_status(status)
        assert truth is not None
        assert truth.stand_in_for is None and not truth.route_unsure, source
        assert all(not d.stand_in_for and not d.route_unsure for d in truth.displays), source


def test_l3_a_false_flag_infers_nothing() -> None:
    status = _at_the_ceiling(_independent("default"))
    status["stand_in_for_omitted"] = False
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.displays[0].stand_in_for is None


def test_l3_the_flag_comes_with_every_stand_in_gone_and_every_pick_hedged() -> None:
    # The service drops every row's stand_in_for together (runtime.rs abridge_rows).
    status = _at_the_ceiling(_independent_two(second_stands_in_for=None))
    assert all("stand_in_for" not in row for row in status["displays"])
    truth = runtime_truth.from_status(status)
    assert truth is not None
    first, second = truth.displays
    assert first.stand_in_for is None and second.stand_in_for is None
    assert first.route_unsure and second.route_unsure


# --------------------------------------------- ceiling flags (service b23081c) --


def _rule_ids_left_out(status: dict[str, Any]) -> dict[str, Any]:
    status = copy.deepcopy(status)
    status["schedule_rule_id_omitted"] = True
    for row in status["displays"]:
        row["schedule_rule_id"] = None
    return status


def test_rule_ids_left_out_an_independent_rule_route_hedges() -> None:
    status = _rule_ids_left_out(_at_the_ceiling(_independent("schedule")))
    truth = runtime_truth.from_status(status)
    assert truth is not None
    [row] = truth.displays
    # No rule id, so no rule to look up: not guessed from the top-level playlist.
    assert row.route_unsure and row.stand_in_for is None
    plays, why = _displays_rows(status)
    assert plays == "Not in use now · “all-media” is playing", plays
    assert "scheduled" not in plays and "pages" not in plays
    assert why == "“all-media” is playing", why
    text, playing, _showing = _on_screen(status, "all-media")
    assert text == f"On screen now · {runtime_truth.SCHEDULE_OR_STAND_IN}", text
    assert not playing


def test_control_rule_ids_present_the_rule_route_is_still_inferred() -> None:
    truth = runtime_truth.from_status(_at_the_ceiling(_independent("schedule")))
    assert truth is not None
    assert truth.displays[0].stand_in_for == "pages" and not truth.displays[0].route_unsure


def test_rule_ids_left_out_a_mirrored_rule_route_hedges_like_an_independent_one() -> None:
    # The row's rule is unknown, and the top-level schedule is what is chosen now, not
    # the row's decision: the row says only what plays.
    status = _at_the_ceiling(copy.deepcopy(_status("pick-refused")))
    status["source"] = "schedule"
    status["schedule"] = {
        "following": True,
        "playlist": "pages",
        "playlist_id": "pages",
        "rule_id": "r1",
    }
    status["displays"][0].update(
        route_source="schedule", manual_override=False, schedule_rule_id="r1"
    )
    status = _rule_ids_left_out(status)
    truth = runtime_truth.from_status(status)
    assert truth is not None
    assert truth.displays[0].stand_in_for is None and truth.displays[0].route_unsure


def test_a_mirrored_pick_without_a_rule_id_is_still_a_pick() -> None:
    from wall_in_one.ui.next.real_schedule import RealScheduling

    status = _rule_ids_left_out(_at_the_ceiling(_status("pick-refused")))
    truth = runtime_truth.from_status(status)
    assert truth is not None
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    assert route.route == "pick" and route.rule_id is None
    # Mirrored, it is certainly stood in for (N3), so it isn't hedged.
    assert route.stand_in_for and not route.route_unsure


def test_an_independent_pick_without_a_rule_id_is_still_a_hedged_pick() -> None:
    from wall_in_one.ui.next.real_schedule import RealScheduling

    status = _rule_ids_left_out(_at_the_ceiling(_independent("manual")))
    truth = runtime_truth.from_status(status)
    assert truth is not None
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
    assert route.route == "pick" and route.rule_id is None and route.route_unsure


def _left_out(status: dict[str, Any]) -> dict[str, Any]:
    status = copy.deepcopy(status)
    status["diagnostics_omitted"] = True
    return status


def test_diagnostics_left_out_the_next_status_line_doesnt_say_no_error() -> None:
    from wall_in_one.ui.next import status_line

    model = RuntimeStatusModel()
    model.adopt(_left_out(_status("pick-plays")))
    assert status_line.describe(model.view).endswith(runtime_truth.DIAGNOSTICS_LEFT_OUT)


def test_control_diagnostics_present_the_next_status_line_is_unchanged() -> None:
    from wall_in_one.ui.next import status_line

    model = RuntimeStatusModel()
    model.adopt(_status("pick-plays"))
    assert runtime_truth.DIAGNOSTICS_LEFT_OUT not in status_line.describe(model.view)


@pytest.mark.parametrize("omitted", [True, False])
def test_diagnostics_left_out_the_classic_summary(
    omitted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gtk_helpers import destroy_window
    from tests.test_ui_missing_renderer_notice import _classic as classic_application
    from wall_in_one.ui.window import MainWindow

    application = classic_application(tmp_path, monkeypatch)
    window = MainWindow(application, application.settings)  # type: ignore[arg-type, attr-defined]
    monkeypatch.setattr(window, "report", lambda _text: None)
    try:
        status = _status("pick-plays")
        window.show_runtime_status(_left_out(status) if omitted else status)
        said = runtime_truth.DIAGNOSTICS_LEFT_OUT in window._runtime_summary
        assert said is omitted, window._runtime_summary
    finally:
        destroy_window(window)
        application.session.shutdown()  # type: ignore[attr-defined]
