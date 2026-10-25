"""r13i UI fixes (r13g U-L1, U-L2, U-N1 to U-N5): the behaviour each one asks for."""

from __future__ import annotations

import copy
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

from tests.test_next_schedule_mapping import _playlist  # noqa: E402
from tests.test_ui_next_displays import Shown, backend, items, monitors  # noqa: E402,F401
from tests.test_ui_r13g import (  # noqa: E402
    _app_with_playlists,
    _at_the_ceiling,
    _mirrored_pick_rows,
    _routes,
    _shown_item,
    _status,
)
from wall_in_one.ui import runtime_truth, unnamed_pick  # noqa: E402

PLACEHOLDER = unnamed_pick.UNNAMED_PICK


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover
        pytest.skip("no display")
    Adw.init()


def _renamed(status: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """``status`` with the JSON strings ``"pages"``/``"scenes"``/... swapped for ``names``."""
    pattern = "|".join(re.escape(json.dumps(key)) for key in names)
    text = re.sub(
        pattern,
        lambda m: json.dumps(names[json.loads(m.group(0))]),
        json.dumps(status),
    )
    found = json.loads(text)
    assert isinstance(found, dict)
    return found


# ===================================================================== P1 ======
# On a new model (a playlist created, removed or renamed into another order) the row
# keeps the playlist it was on by id, not by index, and the unnamed-pick item stays the
# item rather than turning into a real playlist.


def _next_after_change(
    status: dict[str, Any],
    before: list[str],
    shown_before: int | None,
    after: list[str],
    *,
    placeholder: bool = False,
) -> str:
    """The next Displays page's "Playing now" row after its playlists change."""
    from wall_in_one.ui.next.displays import DisplaysPage

    [route, *_rest] = _routes(status)
    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow the schedule", *before]))
    if placeholder:
        unnamed_pick.show(row)
    elif shown_before is not None:
        row.set_selected(shown_before)
    fake = MagicMock()
    fake.play_row = row
    fake._play_ids = ["", *before]
    fake.state.playlists = [SimpleNamespace(id=pid, name=pid, automatic=False) for pid in after]
    fake.scheduling.runtime_blocked.return_value = ""
    fake._route.return_value = route
    fake._shown.return_value = None
    fake._status.return_value = ("Playing", "media-playback-start-symbolic", False)
    fake.why.return_value = "why"
    fake._play_selected = lambda r, ids: DisplaysPage._play_selected(fake, r, ids)
    display = SimpleNamespace(
        connector=str(status["displays"][0]["connector"]), model="", placed=False
    )
    print(f"before={_shown_item(row)!r} of {before}; after the change: {after}")
    DisplaysPage._refresh_details(fake, display, True)  # type: ignore[arg-type]
    shown = _shown_item(row)
    print(f"after={shown!r}")
    return shown


def test_p1_next_the_unnamed_pick_item_isnt_turned_into_a_new_playlist() -> None:
    # The pick is unnamed (mirrored ceiling); the row says so. The person creates a
    # playlist "zebra": the row now says "zebra" is what plays now.
    shown = _next_after_change(
        _at_the_ceiling(_mirrored_pick_rows()),
        ["pages", "scenes"],
        None,
        ["pages", "scenes", "zebra"],
        placeholder=True,
    )
    assert shown == PLACEHOLDER, f"a playlist made a moment ago is shown as the pick: {shown!r}"


def test_p1_next_a_row_left_on_a_playlist_isnt_moved_onto_another() -> None:
    # The row was on "scenes" (left there by the person, or by an earlier status).
    # A playlist "aardvark" is listed first: the same index is now "pages".
    shown = _next_after_change(
        _at_the_ceiling(_mirrored_pick_rows()),
        ["pages", "scenes"],
        2,
        ["aardvark", "pages", "scenes"],
    )
    assert shown in ("scenes", PLACEHOLDER), f"the row moved onto {shown!r}"


def test_control_p1_without_a_model_change_the_row_stays() -> None:
    shown = _next_after_change(
        _at_the_ceiling(_mirrored_pick_rows()), ["pages", "scenes"], 2, ["pages", "scenes"]
    )
    assert shown == "scenes"


def test_control_p1_a_named_pick_is_placed_by_its_id_after_a_change() -> None:
    shown = _next_after_change(
        _mirrored_pick_rows(), ["pages", "scenes"], 0, ["aardvark", "pages", "scenes"]
    )
    assert shown == "pages"


def _mirrored_ceiling_on_layout(names: dict[str, str]) -> dict[str, Any]:
    """The tip's two-row mirrored ceiling reply on the harness's DP-1 and HDMI-A-1."""
    status = _mirrored_pick_rows()
    status["displays"][1]["connector"] = "HDMI-A-1"
    return _renamed(_at_the_ceiling(status), names)


def test_p1_real_page_a_new_playlist_isnt_shown_as_the_unnamed_pick(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    scenes = _playlist(session, "Scenes", items[1])
    view = Shown(backend)
    try:
        view.answer(_mirrored_ceiling_on_layout({"pages": pages, "scenes": scenes}))
        view.select("DP-1")
        page = view.page
        before = _shown_item(page.play_row)
        print(f"before: {before!r} of {page._play_ids}")
        assert before == PLACEHOLDER  # precondition: the tip's own N4 behaviour
        _playlist(session, "Zebra", items[2])
        view.state.reload_playlists()
        page.refresh()
        after = _shown_item(page.play_row)
        print(f"after a playlist is made: {after!r} of {page._play_ids}; sent={backend.sent}")
        assert after == PLACEHOLDER, f"the new playlist {after!r} is shown as playing now"
    finally:
        view.close()


# ===================================================================== P2 ======
# Independent, a pick the reply can't tell from its stand-in (route_unsure) is held by
# no connector: neither delete warning claims it, and the delete releases nothing.


def _independent_unsure(ids: dict[str, str]) -> dict[str, Any]:
    from tests.test_ui_next_r13e import _independent

    status = _at_the_ceiling(_independent("manual", asked="pages", plays="scenes"))
    assert status.get("stand_in_for_omitted") is True
    return _renamed(status, ids)


def _independent_named(ids: dict[str, str]) -> dict[str, Any]:
    from tests.test_ui_next_r13e import _independent

    # The pick "scenes" itself plays (no stand-in): named.
    return _renamed(_independent("manual", asked="scenes", plays="scenes"), ids)


def _warnings(application: Any, pid: str) -> tuple[str, str]:
    from wall_in_one.ui import playlists_page
    from wall_in_one.ui.next.real_playlists import RealPlaylists

    session = application.session
    truth = runtime_truth.from_status(getattr(application, "runtime_status", None))
    classic = playlists_page._deletion_body(session, session.playlists.get(pid), truth)
    fake = SimpleNamespace(
        _backend=SimpleNamespace(status_model=application._status_model),
        _session=session,
        _editable=lambda _pid: session.playlists.get(pid),
        _saved_default=lambda: "",
    )
    after = RealPlaylists.delete_warning(fake, pid)  # type: ignore[arg-type]
    print(f"classic={classic!r}\nnext={after!r}")
    return classic, after


def _released(application: Any, monkeypatch: pytest.MonkeyPatch, pid: str, name: str) -> bool:
    from wall_in_one.ui.app import _PlaylistDeleteResult

    released: list[bool] = []

    def release() -> bool:
        released.append(True)
        return True

    monkeypatch.setattr(application, "resume_schedule_async", release)
    failures = application._adopt_playlist_delete(_PlaylistDeleteResult(pid, name))
    print(f"delete released={bool(released)} failures={failures}")
    return bool(released)


def test_p2_the_warnings_dont_claim_an_unsure_pick_for_what_may_be_its_stand_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        application._status_model.adopt(_independent_unsure(ids))
        truth = runtime_truth.from_status(application.runtime_status)
        assert truth is not None and truth.displays[0].route_unsure  # precondition
        classic, after = _warnings(application, ids["scenes"])
        # What the delete does: no release (whole False, connectors ignored); the
        # service keeps a pick of "pages", whose playlist stays.
        assert not _released(application, monkeypatch, ids["scenes"], "scenes")
        assert "manual override" not in classic, classic
        assert "manual override" not in after, after
    finally:
        _close(application)


def test_control_p2_a_named_independent_pick_is_named_by_both_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        application._status_model.adopt(_independent_named(ids))
        classic, after = _warnings(application, ids["scenes"])
        assert "the current manual override" in classic
        assert "the current manual override" in after
    finally:
        _close(application)


# ===================================================================== P3 ======
# Both delete warnings and the delete judge the pick whatever the service state, so a
# kept snapshot (mark_unavailable(forget=False)) is judged like a live one.


def _pick_refused_named(ids: dict[str, str]) -> dict[str, Any]:
    # pick-refused.json: the pick "pages" is refused and "scenes" stands in (named).
    return _renamed(copy.deepcopy(_status("pick-refused")), ids)


def test_p3_the_next_warning_says_what_the_delete_does_with_a_kept_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        application._status_model.adopt(_pick_refused_named(ids))
        application._status_model.mark_unavailable(forget=False)
        view = application._status_model.view
        assert view.service == "unavailable" and view.status is not None  # precondition
        classic, after = _warnings(application, ids["pages"])
        released = _released(application, monkeypatch, ids["pages"], "pages")
        assert ("manual override" in classic) == ("manual override" in after), (
            "the two delete warnings disagree on one snapshot"
        )
        assert ("manual override" in after) == released, (
            f"the next warning {'names' if 'manual override' in after else 'omits'} the "
            f"override; the delete {'releases' if released else 'keeps'} it"
        )
    finally:
        _close(application)


def test_control_p3_while_running_both_warnings_and_the_delete_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = {one.name: one.id for one in application.session.playlists.all()}
        application._status_model.adopt(_pick_refused_named(ids))
        classic, after = _warnings(application, ids["pages"])
        released = _released(application, monkeypatch, ids["pages"], "pages")
        assert "manual override" in classic and "manual override" in after and released
    finally:
        _close(application)


# ===================================================================== P4 ======
# The next UI lists no "Your pick" (real_state.py), so a pick of it is the item
# "Your pick", never row 0, "Follow the schedule".


def test_p4_real_page_your_pick_isnt_shown_as_follow_the_schedule(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[1].path)
    status = _renamed(_mirrored_pick_rows_playing(), {"pages": QUICK_CHOICE_ID, "scenes": pages})
    view = Shown(backend)
    try:
        view.answer(status)
        view.select("DP-1")
        page = view.page
        route = page._route("DP-1")
        print(f"route={route!r}")
        shown = _shown_item(page.play_row)
        print(f"row={shown!r} subtitle={page.play_row.get_subtitle()!r}")
        assert route is not None and route.route == "pick" and route.quick  # precondition
        assert shown != "Follow the schedule", (
            "the person's own pick (Use as wallpaper) holds; the row says Follow the schedule"
        )
    finally:
        view.close()


def _mirrored_pick_rows_playing() -> dict[str, Any]:
    """pick-plays.json (the pick "pages" plays) on DP-1 and HDMI-A-1, mirrored."""
    status = copy.deepcopy(_status("pick-plays"))
    [row] = status["displays"]
    status["displays"] = [{**row, "connector": connector} for connector in ("DP-1", "HDMI-A-1")]
    return status


def test_control_p4_real_page_a_named_pick_is_the_row(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    scenes = _playlist(session, "Scenes", items[1])
    view = Shown(backend)
    try:
        view.answer(_renamed(_mirrored_pick_rows_playing(), {"pages": pages, "scenes": scenes}))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == "Pages"
    finally:
        view.close()


# ===================================================================== P5 ======
# The unnamed-pick item is known by a mark on the row's model, not by its text: a
# playlist named like it, last in the list, is not dropped.


def test_p5_a_playlist_named_like_the_item_isnt_dropped() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow schedule", "pages", PLACEHOLDER]))
    SchedulesPage._place_playback(row, 1)  # a named pick of "pages"
    count = row.get_model().get_n_items()  # type: ignore[union-attr]
    print(f"items after placing a named pick: {count}")
    assert count == 3, "the playlist named like the item is gone from the row"


def test_control_p5_the_item_itself_is_dropped() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow schedule", "pages", "scenes"]))
    unnamed_pick.show(row)
    SchedulesPage._place_playback(row, 1)
    assert row.get_model().get_n_items() == 3 and _shown_item(row) == "pages"  # type: ignore[union-attr]


# ===================================================================== P6 ======
# Where the service names the pick, its answer wins over the app's record: deleting
# "Quick choice" doesn't release the person's current pick of another playlist.


def _record_is_quick_pick_is_pages(application: Any) -> dict[str, str]:
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    session = application.session
    first = session.playlists.by_name("pages").entries[0].path
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, first)
    session.use_playlist(QUICK_CHOICE_ID)  # "Use as wallpaper": the record
    ids = {one.name: one.id for one in session.playlists.all()}
    # Then "pages" through the combo: the service's pick, named by its reply.
    status = _renamed(
        copy.deepcopy(_status("pick-plays")), {"pages": ids["pages"], "scenes": ids["scenes"]}
    )
    application._status_model.adopt(status)
    truth = runtime_truth.from_status(application.runtime_status)
    assert truth is not None and truth.is_manual and truth.asked_playlist_id == ids["pages"]
    assert session.manual_playlist == QUICK_CHOICE_ID  # precondition: the stale record
    return ids


def test_p6_deleting_quick_choice_doesnt_release_the_pick_the_service_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        _record_is_quick_pick_is_pages(application)
        classic, _next = _warnings(application, QUICK_CHOICE_ID)
        released = _released(application, monkeypatch, QUICK_CHOICE_ID, QUICK_CHOICE_NAME)
        assert not released, "the pick of 'pages' is released by deleting 'Quick choice'"
        assert "manual override" not in classic, classic
    finally:
        _close(application)


def test_control_p6_deleting_the_playlist_the_service_names_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        ids = _record_is_quick_pick_is_pages(application)
        assert _released(application, monkeypatch, ids["pages"], "pages")
    finally:
        _close(application)


# ============================================================== U-N6 =========
# A quick pick the reply names is the item "Your pick"; "not named by this reply" is
# said only of a pick the reply cannot name.


def _quick_pick_page(backend: Any, items: Any) -> tuple[Shown, Any]:  # noqa: F811
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[1].path)
    status = _renamed(_mirrored_pick_rows_playing(), {"pages": QUICK_CHOICE_ID, "scenes": pages})
    view = Shown(backend)
    view.answer(status)
    view.select("DP-1")
    route = view.page._route("DP-1")
    assert route is not None and route.route == "pick" and route.quick  # precondition
    assert not route.pick_stood_in and not route.route_unsure  # precondition: named
    return view, route


def test_u_n6_a_named_quick_pick_isnt_said_to_be_unnamed_by_the_reply(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, route = _quick_pick_page(backend, items)
    try:
        page = view.page
        shown = _shown_item(page.play_row)
        print(f"route={route!r}\nrow={shown!r} subtitle={page.play_row.get_subtitle()!r}")
        assert shown != "Follow the schedule", "U-L2 itself"
        assert shown == "Your pick", f"the reply names the pick; the row says {shown!r}"
    finally:
        view.close()


def test_control_u_n6_a_pick_the_reply_cant_name_shows_the_unnamed_item(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    scenes = _playlist(session, "Scenes", items[1])
    view = Shown(backend)
    try:
        view.answer(_mirrored_ceiling_on_layout({"pages": pages, "scenes": scenes}))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == PLACEHOLDER
    finally:
        view.close()


# ============================================================== U-L-a ========
# The next UI's play_ids leave out every automatic playlist, All media included. A pick
# of one is the item "Your pick", never row 0, "Follow the schedule".


def _all_media_pick(pages: str) -> dict[str, Any]:
    # pick-plays.json with the pick "pages" -> "all-media" (the inventory's own
    # all-media row renamed out of the way), "scenes" -> the session's Pages.
    return _renamed(
        _mirrored_pick_rows_playing(),
        {"pages": "all-media", "all-media": "gone", "scenes": pages},
    )


def test_u_l_a_an_all_media_pick_isnt_shown_as_follow_the_schedule(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    view = Shown(backend)
    try:
        view.answer(_all_media_pick(pages))
        view.select("DP-1")
        page = view.page
        route = page._route("DP-1")
        print(f"route={route!r}")
        assert route is not None and route.route == "pick"  # precondition
        assert route.playlist_id == "all-media" and not route.quick  # precondition
        shown = _shown_item(page.play_row)
        print(f"row={shown!r} subtitle={page.play_row.get_subtitle()!r} ids={page._play_ids}")
        assert shown == "Your pick", f"a pick of All media holds; the row says {shown!r}"
    finally:
        view.close()


def test_u_l_a_unit_play_selected_doesnt_put_an_unlisted_pick_on_row_0() -> None:
    from wall_in_one.ui.next.displays import DisplaysPage

    [route, *_rest] = _routes(_all_media_pick("p"))
    assert route.route == "pick" and route.playlist_id == "all-media"  # precondition
    selected = DisplaysPage._play_selected(SimpleNamespace(), route, ["", "p"])  # type: ignore[arg-type]
    assert selected is None, "row 0 is Follow the schedule"


def test_control_u_l_a_a_listed_pick_is_its_row(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    session = backend.session
    pages = _playlist(session, "Pages", items[0])
    scenes = _playlist(session, "Scenes", items[1])
    view = Shown(backend)
    try:
        view.answer(_renamed(_mirrored_pick_rows_playing(), {"pages": pages, "scenes": scenes}))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == "Pages"
    finally:
        view.close()


# ============================================================== U-L-b ========
# A running service that says no pick holds is a definite answer: the app's record of a
# pick (Use as wallpaper, never cleared by a resume through the service) doesn't count.
# Both delete warnings and the delete's schedule-follow read it through pick_holders.


def _following(ids: dict[str, str], *, independent: bool = False) -> dict[str, Any]:
    """pick-plays.json with the pick released: every display on its default "scenes"."""
    status = copy.deepcopy(_status("pick-plays"))
    status["source"] = "schedule"
    status["playlist_id"] = status["playlist"] = "scenes"
    status["entry_id"] = "scenes-1"
    status["schedule"] = {
        "following": True,
        "playlist": "scenes",
        "playlist_id": "scenes",
        "rule_id": None,
    }
    status.pop("stand_in_for", None)
    for playlist in status["playlists"]:
        playlist["active"] = playlist["id"] == "scenes"
    [row] = status["displays"]
    row.pop("stand_in_for", None)
    row.update(
        route_source="default",
        manual_override=False,
        playlist="scenes",
        playlist_id="scenes",
        entry_id="scenes-1",
        assigned_playlist="scenes",
        assigned_playlist_id="scenes",
    )
    if independent:
        status["display_mode"] = "independent"
        row["connector"] = "DP-1"
    return _renamed(status, ids)


def _quick_record(application: Any) -> str:
    from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME

    session = application.session
    first = session.playlists.by_name("pages").entries[0].path
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, first)
    session.use_playlist(QUICK_CHOICE_ID)  # "Use as wallpaper": the record
    assert session.manual_playlist == QUICK_CHOICE_ID  # precondition
    return QUICK_CHOICE_ID


@pytest.mark.parametrize("independent", [False, True], ids=["mirrored", "independent"])
def test_u_l_b_a_running_service_that_says_no_pick_beats_a_stale_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent: bool
) -> None:
    from tests.test_ui_runtime import _close
    from wall_in_one.session import QUICK_CHOICE_NAME

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        quick = _quick_record(application)
        ids = {one.name: one.id for one in application.session.playlists.all()}
        # "Resume schedule" through the service: the status says no pick holds; the
        # record is unchanged (resume_schedule_async doesn't touch it).
        application._status_model.adopt(_following(ids, independent=independent))
        view = application._status_model.view
        truth = view.truth
        assert truth is not None and not truth.is_manual  # precondition
        assert not any(row.manual_override for row in truth.displays)  # precondition
        assert truth.display_mode == ("independent" if independent else "mirrored")
        assert not truth.route_unsure and not truth.pick_stood_in  # not the ceiling
        print(f"holders={runtime_truth.pick_holders(truth, quick, quick)}")
        classic, _next = _warnings(application, quick)
        released = _released(application, monkeypatch, quick, QUICK_CHOICE_NAME)
        assert "manual override" not in classic, classic
        assert not released, "the delete sends schedule-follow though no pick holds"
    finally:
        _close(application)


def test_control_u_l_b_the_quick_pick_the_service_names_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close
    from wall_in_one.session import QUICK_CHOICE_NAME

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        quick = _quick_record(application)
        ids = {one.name: one.id for one in application.session.playlists.all()}
        status = _renamed(
            copy.deepcopy(_status("pick-plays")), {"pages": quick, "scenes": ids["scenes"]}
        )
        application._status_model.adopt(status)
        classic, _next = _warnings(application, quick)
        assert "the current manual override" in classic
        assert _released(application, monkeypatch, quick, QUICK_CHOICE_NAME)
    finally:
        _close(application)


def test_control_u_l_b_with_no_status_the_record_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ui_runtime import _close
    from wall_in_one.session import QUICK_CHOICE_NAME

    application, _window, _items = _app_with_playlists(tmp_path, monkeypatch)
    try:
        quick = _quick_record(application)
        assert application.runtime_status is None  # precondition: nothing heard
        assert _released(application, monkeypatch, quick, QUICK_CHOICE_NAME)
    finally:
        _close(application)


def test_control_u_l_b_independent_unsure_manual_row_keeps_the_record() -> None:
    status = _independent_unsure({"pages": "p", "scenes": "s"})
    assert _holders(status, "s", "s") == (True, ())
    assert _holders(status, None, "s") == (False, ())


def test_u_l_b_pick_holders_a_service_that_says_no_pick_beats_the_record() -> None:
    for independent in (False, True):
        status = _following({"pages": "p", "scenes": "s"}, independent=independent)
        assert _holders(status, "p", "p") == (False, ())


# ===================================================== the classic twins of U-L-a, U-N6 =====


def _all_media_rows_pick() -> dict[str, Any]:
    return _renamed(_mirrored_pick_rows(), {"pages": "all-media", "all-media": "gone"})


def test_classic_overall_combo_an_all_media_pick_isnt_follow_schedule() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = runtime_truth.from_status(_all_media_rows_pick())
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    session = SimpleNamespace(manual_playlist=None)
    assert truth is not None and truth.is_manual and truth.asked_playlist_id == "all-media"
    assert SchedulesPage._playback_selected(session, choices, truth) is None  # type: ignore[arg-type]


def test_control_classic_overall_combo_a_listed_pick_is_its_row() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = runtime_truth.from_status(_mirrored_pick_rows())
    choices = (SimpleNamespace(id="scenes"), SimpleNamespace(id="pages"))
    session = SimpleNamespace(manual_playlist=None)
    assert SchedulesPage._playback_selected(session, choices, truth) == 2  # type: ignore[arg-type]


def test_classic_overall_combo_a_named_unlisted_pick_is_your_pick_after_a_click() -> None:
    from tests.test_ui_r13g import _classic_changed

    shown = _classic_changed(_all_media_rows_pick(), 1)
    assert shown == "Your pick", f"the reply names the pick; the row says {shown!r}"


def test_control_classic_overall_combo_an_unnamed_pick_keeps_the_not_named_item() -> None:
    from tests.test_ui_r13g import _classic_changed

    assert _classic_changed(_at_the_ceiling(_mirrored_pick_rows()), 0) == PLACEHOLDER


def _independent_all_media_pick() -> dict[str, Any]:
    return _independent_named({"scenes": "all-media", "all-media": "gone"})


def test_classic_display_row_an_all_media_pick_isnt_follow_schedule() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = runtime_truth.from_status(_independent_all_media_pick())
    assert truth is not None
    [display] = truth.displays
    assert display.manual_override and display.asked_playlist_id == "all-media"
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    selected = SchedulesPage._display_playback_selected(display, choices)
    assert selected is None
    from wall_in_one.ui.schedules_page import _display_pick_label

    row = Adw.ComboRow(model=Gtk.StringList.new(["Follow schedule", "pages", "scenes"]))
    fake = SimpleNamespace(_loading=False, _display_selected_echo={})
    SchedulesPage._set_display_selected(fake, "DP-1", row, selected, _display_pick_label(display))  # type: ignore[arg-type]
    assert _shown_item(row) == "Your pick"


def test_control_classic_display_row_a_listed_pick_is_its_row() -> None:
    from wall_in_one.ui.schedules_page import SchedulesPage

    truth = runtime_truth.from_status(_independent_named({"scenes": "scenes"}))
    assert truth is not None
    [display] = truth.displays
    choices = (SimpleNamespace(id="pages"), SimpleNamespace(id="scenes"))
    assert SchedulesPage._display_playback_selected(display, choices) == 2


# ============================================================ pick_holders (U-N5) =====


def _holders(
    status: dict[str, Any] | None, record: str | None, pid: str
) -> tuple[bool, tuple[str, ...]]:
    return runtime_truth.pick_holders(runtime_truth.from_status(status), record, pid)


def test_pick_holders_the_service_names_a_pick_the_record_doesnt() -> None:
    status = _renamed(copy.deepcopy(_status("pick-plays")), {"pages": "p", "scenes": "s"})
    assert _holders(status, "s", "s")[0] is False
    assert _holders(status, "s", "p")[0] is True


def test_pick_holders_no_status_uses_the_record() -> None:
    assert _holders(None, "p", "p") == (True, ())
    assert _holders(None, "p", "s") == (False, ())


def test_pick_holders_at_the_reply_ceiling_uses_the_record() -> None:
    status = _renamed(_at_the_ceiling(_mirrored_pick_rows()), {"pages": "p", "scenes": "s"})
    assert _holders(status, "s", "s")[0] is True
    assert _holders(status, None, "s")[0] is False


def test_pick_holders_independent_named_pick_beats_the_record() -> None:
    status = _independent_named({"pages": "p", "scenes": "s"})
    whole, connectors = _holders(status, "p", "p")
    assert not whole and not connectors
    assert _holders(status, "p", "s")[1]


def test_pick_holders_independent_unsure_pick_names_nothing() -> None:
    status = _independent_unsure({"pages": "p", "scenes": "s"})
    assert _holders(status, None, "s") == (False, ())
    assert _holders(status, None, "p") == (False, ())
