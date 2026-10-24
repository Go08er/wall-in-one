"""Both interfaces say a stand-in as one, and mark what the service refused (review r13b).

S1: the review's probe (``review-r13b/test_probe_status_truth.py``) as a test. The
statuses are the service's own (``service/tests/fixtures/stand-in/``): a pick of
``pages`` the service refuses throughout, the default ``scenes`` playing in its
place; and, the control, the same pick when it plays. At b3d1398 the Schedules page
said "Manual override · scenes is playing." and put its playback dropdown on
``scenes``, though the person picked ``pages``.

S2: an item the service refused in the configuration it holds (status ``refused``)
is marked "Can't play here" with the service's reason, in the classic Library's
tile and the new interface's card; never as a playback warning that is saved.
"""

from __future__ import annotations

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

from wall_in_one.library import pairings  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem  # noqa: E402
from wall_in_one.library.scan import WORKSHOP_PROVIDER  # noqa: E402
from wall_in_one.ui import runtime_truth  # noqa: E402
from wall_in_one.ui.schedules_page import SchedulesPage  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/stand-in"
CHOICES = (SimpleNamespace(id="scenes", name="scenes"), SimpleNamespace(id="pages", name="pages"))
STAND_IN = "\u201cpages\u201d can\u2019t play here, so \u201cscenes\u201d is playing"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _truth(name: str) -> runtime_truth.RuntimeTruth:
    status = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    truth = runtime_truth.from_status(status)
    assert truth is not None and truth.is_manual
    return truth


def _shown(name: str) -> tuple[str, str | None]:
    truth = _truth(name)
    said = SchedulesPage._playback_description(None, truth)  # type: ignore[arg-type]
    index = SchedulesPage._playback_selected(None, CHOICES, truth)  # type: ignore[arg-type]
    return said, CHOICES[index - 1].id if index else None


def test_a_pick_the_service_refused_isnt_shown_as_the_stand_in() -> None:
    said, selected = _shown("pick-refused")
    assert selected == "pages", f"the dropdown is on the stand-in: {selected}"
    assert said == f"Manual override · {STAND_IN}.", said


def test_control_a_pick_that_plays_is_shown_as_itself() -> None:
    said, selected = _shown("pick-plays")
    assert selected == "pages"
    assert said == "Manual override · pages is playing."


def test_a_displays_row_says_its_pick_cant_play_and_keeps_its_dropdown() -> None:
    truth = _truth("pick-refused")
    [row] = truth.displays
    assert SchedulesPage._display_playback_selected(row, CHOICES) == 2
    said = SchedulesPage._display_playback_description(row, truth, taboo=False)
    assert said.startswith(f"Manual override · {STAND_IN} · "), said
    plays = _truth("pick-plays")
    [row] = plays.displays
    assert SchedulesPage._display_playback_selected(row, CHOICES) == 2
    assert SchedulesPage._display_playback_description(row, plays, taboo=False).startswith(
        "Manual override · pages · "
    )


def test_the_next_interfaces_route_names_the_pick_beside_its_stand_in() -> None:
    from wall_in_one.ui.next.real_schedule import RealScheduling

    [route] = RealScheduling._read_routes(None, _truth("pick-refused"))  # type: ignore[arg-type]
    assert route.route == "pick" and route.playlist_id == "scenes"
    assert route.stand_in_for == "pages" and route.stand_in_for_name == "pages"
    assert not route.quick
    [control] = RealScheduling._read_routes(None, _truth("pick-plays"))  # type: ignore[arg-type]
    assert control.playlist_id == "pages" and control.stand_in_for == ""


def _labels(widget: Any) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_visible():
            found.append(child.get_label())
        found.extend(_labels(child))
        child = child.get_next_sibling()
    return found


def _tooltips(widget: Any) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        tip = child.get_tooltip_text()
        if child.get_visible() and tip:
            found.append(tip)
        found.extend(_tooltips(child))
        child = child.get_next_sibling()
    return found


def _scene(workshop_id: str, kind: str = "scene") -> MediaItem:
    return MediaItem(
        path=Path("/steam/content/431960") / workshop_id,
        kind=Kind.SCENE,
        size=0,
        mtime=0,
        provider=WORKSHOP_PROVIDER,
        scene=workshop_id,
        title="Far Away",
        workshop_type=kind,
        web_page=kind == "web",
    )


REASON = "Workshop item 2910000004 wasn't found where linux-wallpaperengine looks"


def test_the_classic_tile_marks_an_item_the_service_refused() -> None:
    from wall_in_one.ui.grid import WallpaperTile

    tile = WallpaperTile(_scene("2910000004"))
    assert "Can\u2019t play here" not in _labels(tile)
    tile.set_refused(REASON)
    assert "Can\u2019t play here" in _labels(tile)
    assert f"The wallpaper service skips it: {REASON}" in _tooltips(tile)
    tile.set_refused(None)
    assert "Can\u2019t play here" not in _labels(tile), "the service no longer says so"
    # Beside a mark that already says it never plays here, not said twice.
    web = WallpaperTile(_scene("2777242023", "web"), allow_web=False)
    web.set_refused("this configuration doesn't allow web wallpapers")
    assert "Can\u2019t play" in _labels(web) and "Can\u2019t play here" not in _labels(web)
    # Nor beside a playback warning.
    borked = WallpaperTile(_scene("2910000004"))
    borked.set_borked("the renderer crashed")
    borked.set_refused(REASON)
    assert "Can\u2019t play here" not in _labels(borked)


def test_the_next_card_marks_an_item_the_service_refused() -> None:
    from wall_in_one.ui.next.real_state import wallpaper_view
    from wall_in_one.ui.next.widgets import WallpaperCard

    item = _scene("2910000004")
    view = wallpaper_view(
        item, favourite=False, pairing=None, health=pairings.Health(), refused_here=REASON
    )
    assert view.refused_here == REASON and not view.problem
    card = WallpaperCard(view)
    assert "Can\u2019t play here" in _labels(card)
    assert f"The wallpaper service skips it: {REASON}" in _tooltips(card)
    control = wallpaper_view(item, favourite=False, pairing=None, health=pairings.Health())
    assert "Can\u2019t play here" not in _labels(WallpaperCard(control))
