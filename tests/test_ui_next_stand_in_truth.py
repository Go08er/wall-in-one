"""The new interface and the classic count say a stand-in as one (review r13c).

UI-A: the Playlists detail of the playlist standing in for a refused pick isn't
presented as the person's pick. UI-B: a refused default isn't "In use now". N-1: the
classic " · N can't play here" counts the library's marked items. N-6: the inspector doesn't
promise a recheck. N-7: the new Library says how many refused items it has no card
marked for (``refused_omitted``). Every status may lack ``stand_in_for``, ``refused``
and ``refused_omitted`` (a reply held to its budget).
"""

from __future__ import annotations

import copy
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
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _status(name: str) -> dict[str, Any]:
    status = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(status, dict)
    return status


def _on_screen(status: dict[str, Any], pid: str) -> tuple[str, bool]:
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    model = RuntimeStatusModel()
    model.adopt(status)
    fake = SimpleNamespace(_backend=SimpleNamespace(status_model=model))
    on_screen, playing, _showing = RealPlaylists._on_screen(fake, pid)  # type: ignore[arg-type]
    return on_screen, playing


def test_a_the_stand_in_playlists_detail_isnt_presented_as_the_pick() -> None:
    on_screen, playing = _on_screen(_status("pick-refused"), "scenes")
    assert "your pick" not in on_screen and not playing, (
        "the stand-in `scenes` is presented as the person's pick of `pages`: "
        f"on_screen={on_screen!r}, playing={playing} (button 'Playing now', tooltip "
        "'It is your pick on every display now')"
    )
    assert on_screen == runtime_truth.stand_in_sentence("pages", "scenes")


def test_a_a_summary_only_stand_in_is_said_the_same() -> None:
    # A version-1 runtime: no display rows, one answer for every display.
    status = _status("pick-refused")
    status["displays"] = []
    on_screen, playing = _on_screen(status, "scenes")
    assert "your pick" not in on_screen and not playing, on_screen
    assert on_screen == runtime_truth.stand_in_sentence("pages", "scenes")


def test_a_a_status_without_stand_in_for_is_read_as_before() -> None:
    # A service that sends no ``refused`` at all (older): no claim either way, so
    # the pick is read as the pick. The reply ceiling is a different shape
    # (``refused == []`` with ``refused_omitted > 0``), tested in
    # test_ui_next_r13e.py.
    status = _status("pick-refused")
    for key in ("stand_in_for", "refused", "refused_omitted"):
        status.pop(key, None)
    for row in status["displays"]:
        row.pop("stand_in_for", None)
    assert _on_screen(status, "scenes") == ("On screen now \u00b7 your pick", True)


def test_control_a_a_pick_that_plays_is_its_own_pick() -> None:
    assert _on_screen(_status("pick-plays"), "pages") == ("On screen now · your pick", True)


def test_control_a_the_asked_playlist_says_it_cant_play() -> None:
    on_screen, playing = _on_screen(_status("pick-refused"), "pages")
    assert on_screen.startswith(CANT_PLAY_HERE) and not playing


def test_a_the_controls_survive_a_status_without_refused() -> None:
    status = _status("pick-plays")
    status.pop("refused", None)
    status.pop("refused_omitted", None)
    assert _on_screen(status, "pages") == ("On screen now \u00b7 your pick", True)


# ---------------------------------------------------------------- B ------------


def _independent_default_refused() -> dict[str, Any]:
    """pick-refused.json as an independent display on the default ``pages``, refused
    throughout, with All media standing in (derived, see the module docstring)."""
    status = copy.deepcopy(_status("pick-refused"))
    status["display_mode"] = "independent"
    status["source"] = "schedule"
    status["playlist_id"] = status["playlist"] = "all-media"
    status["stand_in_for"] = "pages"
    status["entry_id"] = "all-media-1"
    status["schedule"] = {
        "following": True,
        "playlist": "pages",
        "playlist_id": "pages",
        "rule_id": None,
    }
    for playlist in status["playlists"]:
        playlist["active"] = playlist["id"] == "all-media"
    [row] = status["displays"]
    row.update(
        connector="DP-1",
        assigned_playlist="pages",
        assigned_playlist_id="pages",
        assignment_source="default",
        manual_override=False,
        route_source="default",
        playlist="all-media",
        playlist_id="all-media",
        stand_in_for="pages",
        entry_id="all-media-1",
    )
    return status


def _displays_rows(status: dict[str, Any]) -> tuple[str, str]:
    from wall_in_one.ui.next.displays import DisplaysPage
    from wall_in_one.ui.next.real_schedule import RealScheduling

    truth = runtime_truth.from_status(status)
    assert truth is not None
    [route] = RealScheduling._read_routes(None, truth)  # type: ignore[arg-type]
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
    DisplaysPage._refresh_plays(fake, "DP-1", "", route)  # type: ignore[arg-type]
    why = DisplaysPage.why(fake, "DP-1")  # type: ignore[arg-type]
    plays = row.get_subtitle() or ""
    return plays, why


def test_b_a_refused_default_isnt_said_to_be_in_use() -> None:
    plays, why = _displays_rows(_independent_default_refused())
    assert CANT_PLAY_HERE.casefold() in why.casefold(), why  # the same page's "why" says so
    assert plays != "In use now", f"'Default (pages)' is said to be 'In use now' while {why!r}"
    assert plays == f"{CANT_PLAY_HERE} \u00b7 \u201call-media\u201d plays in its place", plays


def test_b_a_refused_default_without_stand_in_for_is_not_claimed() -> None:
    status = _independent_default_refused()
    status.pop("stand_in_for")
    status["displays"][0].pop("stand_in_for")
    plays, _why = _displays_rows(status)
    assert CANT_PLAY_HERE not in plays


def test_control_b_a_default_that_plays_is_in_use() -> None:
    status = _independent_default_refused()
    status.pop("stand_in_for")
    status["displays"][0].pop("stand_in_for")
    status["displays"][0].update(playlist="pages", playlist_id="pages")
    status["playlist_id"] = status["playlist"] = "pages"
    plays, why = _displays_rows(status)
    assert plays == "In use now" and CANT_PLAY_HERE.casefold() not in why.casefold()


def test_control_b_a_refused_assignment_says_it_cant_play() -> None:
    status = _independent_default_refused()
    status["displays"][0].update(route_source="assignment", assignment_source="explicit")
    plays, _why = _displays_rows(status)
    assert plays.startswith(CANT_PLAY_HERE), plays


# ---------------------------------------------------------------- C ------------


def _scene(workshop_id: str, *, base: str = "", web: bool = False) -> MediaItem:
    return MediaItem(
        path=Path("/steam/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=0,
        mtime=0,
        provider=WORKSHOP_PROVIDER,
        scene=workshop_id,
        scene_base=base,
        title=f"Item {workshop_id}",
        workshop_type="preset" if base else "web" if web else "scene",
        web_page=web,
    )


class _Library:
    def __init__(self, items: tuple[MediaItem, ...]) -> None:
        self.items = items
        self.videos = ()
        self.stills = ()
        self.skipped = ()

    def __len__(self) -> int:
        return len(self.items)


def _labels(widget: Any) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_visible():
            found.append(child.get_label())
        found.extend(_labels(child))
        child = child.get_next_sibling()
    return found


def _classic(items: tuple[MediaItem, ...], refused: list[dict[str, Any]]) -> tuple[str, int]:
    from wall_in_one.ui.grid import WallpaperGrid
    from wall_in_one.ui.thumbnails import ThumbnailLoader
    from wall_in_one.ui.window import MainWindow

    class Loader(ThumbnailLoader):
        def request(self, item: MediaItem, callback: Any) -> None:
            del item, callback

    grid = WallpaperGrid(Loader(max_workers=1), lambda _item: None)
    grid.populate(items)
    status = {**_status("pick-refused"), "refused": refused, "refused_omitted": 0}
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
    marked = sum(CANT_PLAY_HERE in _labels(tile) for tile in grid._tiles.values())
    return fake._summary, marked


def _said(summary: str) -> int:
    tail = summary.rsplit(" · ", 1)[-1]
    return int(tail.split()[0]) if tail.casefold().endswith(CANT_PLAY_HERE.casefold()) else 0


NOT_FOUND = {
    "scene_id": "2910000004",
    "web_page": False,
    "reason": "Workshop item 2910000004 wasn't found where linux-wallpaperengine looks",
}
WEB = {
    "scene_id": "2777242023",
    "web_page": True,
    "reason": "this configuration doesn't allow web wallpapers: Workshop item 2777242023 is "
    "a web page, so linux-wallpaperengine was not started for it",
}


def test_c_a_preset_and_its_base_are_counted_as_marked() -> None:
    summary, marked = _classic(
        (_scene("2910000004"), _scene("3000000001", base="2910000004")), [NOT_FOUND]
    )
    assert _said(summary) == marked, f"{summary!r} beside {marked} marked tiles"


def test_c_a_web_page_said_to_be_off_isnt_counted_again() -> None:
    summary, marked = _classic((_scene("2777242023", web=True),), [WEB])
    assert _said(summary) == marked, f"{summary!r} beside {marked} marked tiles"


def test_control_c_one_refused_scene_one_mark() -> None:
    summary, marked = _classic((_scene("2910000004"), _scene("12345")), [NOT_FOUND])
    assert _said(summary) == marked == 1, summary


def test_c_the_count_follows_the_marked_tiles_and_the_omitted() -> None:
    _summary, marked = _classic((_scene("2910000004"), _scene("12345")), [NOT_FOUND])
    assert marked == 1
    status = {**_status("pick-refused"), "refused": [NOT_FOUND], "refused_omitted": 3}
    fake = _window_for(status, (_scene("2910000004"), _scene("12345")))
    # One marked item, and three more the service refused and left out: two counts.
    assert fake._summary.endswith(" \u00b7 1 can\u2019t play here, and 3 more refused"), (
        fake._summary
    )
    older = {k: v for k, v in _status("pick-refused").items() if not k.startswith("refused")}
    assert _said(_window_for(older, (_scene("12345"),))._summary) == 0


def _window_for(status: dict[str, Any], items: tuple[MediaItem, ...]) -> Any:
    from wall_in_one.ui.grid import WallpaperGrid
    from wall_in_one.ui.thumbnails import ThumbnailLoader
    from wall_in_one.ui.window import MainWindow

    class Loader(ThumbnailLoader):
        def request(self, item: MediaItem, callback: Any) -> None:
            del item, callback

    grid = WallpaperGrid(Loader(max_workers=1), lambda _item: None)
    grid.populate(items)
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
    return fake


# ---------------------------------------------------------------- N-6 ----------


def _text_of(widget: Any) -> str:
    return " ".join(_labels(widget))


def test_n6_the_inspector_doesnt_promise_a_recheck() -> None:
    from wall_in_one.ui.next.inspector import Inspector

    card = Inspector._refused_here(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(refused_here="not found"),
    )
    text = _text_of(card)
    assert "not found" in text and "skips it" in text  # control: the reason stays
    assert "checks again" not in text and "next published" not in text, text


# ---------------------------------------------------------------- N-7 ----------


def _count_text(omitted: int, *, marked: bool = True) -> str:
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


def test_n7_the_library_says_how_many_more_were_refused() -> None:
    assert "and 5 more can\u2019t play here" in _count_text(5)


def test_n7_control_no_omitted_says_nothing() -> None:
    assert _count_text(0) == "3 wallpapers"


def test_n7_the_state_keeps_the_omitted_count_and_tolerates_its_absence() -> None:
    from wall_in_one.ui.next.real_state import RealAppState

    def adopt(status: dict[str, Any], before: int = 0) -> tuple[str, int]:
        truth = runtime_truth.from_status(status)
        assert truth is not None
        fake = SimpleNamespace(
            _refused={},
            _refused_omitted=before,
            _wallpapers=(),
            _index={},
        )
        fake._refused_reason = lambda item: ""
        changed = RealAppState._adopt_refused(fake, truth)  # type: ignore[arg-type]
        return changed, fake._refused_omitted

    base = _status("pick-refused")
    base.pop("refused", None)
    base.pop("refused_omitted", None)
    assert adopt({**base, "refused": [NOT_FOUND], "refused_omitted": 4})[1] == 4
    assert adopt({**base, "refused": [NOT_FOUND]})[1] == 0
    assert adopt(base) == ("", 0)
    # Only the count moved: the Library hears of the count, not of every card.
    assert adopt({**base, "refused": [], "refused_omitted": 2})[0] == "library-count"


# ---------------------------------------------- Displays: pick and rule stand-ins ---


def _routed(source: str, *, stood: bool) -> str:
    status = _independent_default_refused()
    status["displays"][0].update(route_source=source)
    if not stood:
        status.pop("stand_in_for")
        status["displays"][0].pop("stand_in_for")
        status["displays"][0].update(playlist="pages", playlist_id="pages")
    return _displays_rows(status)[0]


def test_displays_a_pick_that_stood_in_says_it_cant_play() -> None:
    plays = _routed("manual", stood=True)
    assert plays == "Not in use now · " + runtime_truth.stand_in_sentence("pages", "all-media")


def test_displays_a_rule_that_stood_in_says_it_cant_play() -> None:
    plays = _routed("schedule", stood=True)
    assert plays.startswith("Not in use now · ")
    assert runtime_truth.stand_in_sentence("pages", "all-media") in plays


def test_displays_control_a_pick_and_a_rule_that_play_are_said_as_before() -> None:
    assert _routed("manual", stood=False) == "Not in use now · your pick is showing"
    assert _routed("schedule", stood=False).endswith("is scheduled")


# ----------------------------- the classic count follows the web-allowed setting ---


def _toggled(first: bool, then: bool) -> tuple[str, str]:
    from wall_in_one.ui.grid import WallpaperGrid
    from wall_in_one.ui.thumbnails import ThumbnailLoader
    from wall_in_one.ui.window import MainWindow

    class Loader(ThumbnailLoader):
        def request(self, item: MediaItem, callback: Any) -> None:
            del item, callback

    items = (_scene("2777242023", web=True),)
    grid = WallpaperGrid(Loader(max_workers=1), lambda _item: None)
    grid.set_web_allowed(first)
    grid.populate(items)
    fake = MagicMock()
    fake._grid = grid
    fake._runtime_media_status = {**_status("pick-refused"), "refused": [WEB]}
    fake._borked_count = 0
    fake._summary = ""
    fake._settings = SimpleNamespace(allow_web_wallpapers=first, roots=())
    fake._management_session = SimpleNamespace(
        library=_Library(items),
        playlists=SimpleNamespace(all=lambda: (), get=lambda _pid: None),
        active_playlist=lambda: "",
        cursor=None,
    )
    fake._session_playing_label = MainWindow._session_playing_label
    fake._show_media_playback = lambda session: MainWindow._show_media_playback(fake, session)
    MainWindow._show_media_playback(fake, fake._management_session)
    before = fake._summary
    new = SimpleNamespace(
        allow_web_wallpapers=then,
        display_mode="mirrored",
        roots=(),
    )
    MainWindow.apply_settings(fake, new)  # type: ignore[arg-type]
    return before, fake._summary


def test_a_web_toggle_recounts_the_summary() -> None:
    before, after = _toggled(False, True)
    assert _said(before) == 0  # web off: the tile says so, and isn't counted
    assert _said(after) == 1, after  # web on: the tile is marked "Can't play here"


def test_a_web_toggle_off_recounts_the_summary() -> None:
    before, after = _toggled(True, False)
    assert _said(before) == 1 and _said(after) == 0, (before, after)


def test_control_an_unchanged_web_setting_leaves_the_summary() -> None:
    before, after = _toggled(True, True)
    assert _said(before) == _said(after) == 1
