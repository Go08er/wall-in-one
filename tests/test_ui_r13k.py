"""r13k: the Active-playlist label ("Your pick" or the not-named item) follows the reply.

U-N9, U-N10 and U-N11 each have a test that failed at ed7c249 and a control. The U-N12
section covers the five label clauses r13j found untested, each with a control. U-N15
(the next page's labels) and U-L-c (one click, one command, and a spent echo) follow it.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.test_next_schedule_mapping import _playlist  # noqa: E402
from tests.test_next_state import _session  # noqa: E402
from tests.test_ui_next_displays import Shown, backend, items, monitors  # noqa: E402,F401
from tests.test_ui_r13g import _at_the_ceiling, _mirrored_pick_rows, _shown_item  # noqa: E402
from tests.test_ui_r13i import (  # noqa: E402
    _following,
    _independent_named,
    _independent_unsure,
    _mirrored_ceiling_on_layout,
    _mirrored_pick_rows_playing,
    _renamed,
)
from wall_in_one import config  # noqa: E402
from wall_in_one.ui import schedules_page, unnamed_pick  # noqa: E402

UNNAMED = unnamed_pick.UNNAMED_PICK
NAMED = unnamed_pick.NAMED_PICK


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover
        pytest.skip("no display")
    Adw.init()


def _all_media_named(ids: dict[str, str]) -> dict[str, Any]:
    """Mirrored, two rows (DP-1, HDMI-A-1): a pick of All media that plays (named)."""
    return _renamed(
        _mirrored_pick_rows_playing(),
        {"pages": "all-media", "all-media": "gone", "scenes": ids["scenes"]},
    )


def _ceiling(ids: dict[str, str]) -> dict[str, Any]:
    """Mirrored, two rows: the pick can't be named (pick_stood_in at the ceiling)."""
    return _mirrored_ceiling_on_layout({"pages": ids["pages"], "scenes": ids["scenes"]})


def _following_rows(ids: dict[str, str]) -> dict[str, Any]:
    """Mirrored, DP-1 + HDMI-A-1, no pick: every display on its default "Scenes"."""
    status = _following(ids, independent=False)
    [row] = status["displays"]
    status["displays"] = [{**row, "connector": one} for one in ("DP-1", "HDMI-A-1")]
    return status


def _independent_all_media(ids: dict[str, str]) -> dict[str, Any]:
    return _independent_named({"scenes": "all-media", "all-media": "gone", "pages": ids["pages"]})


def _independent_ceiling(ids: dict[str, str]) -> dict[str, Any]:
    return _independent_unsure({"pages": ids["pages"], "scenes": ids["scenes"]})


class _App:
    """What SchedulesPage asks of the application; every runtime command is recorded."""

    def __init__(self, session: Any, status: dict[str, Any] | None) -> None:
        self.session = session
        self.runtime_status = status
        self.sent: list[tuple[str, ...]] = []
        self.on_command: Any = None  # runs inside a command, before it returns

    def _record(self, *what: str) -> bool:
        self.sent.append(what)
        if self.on_command is not None:
            self.on_command()
        return True

    def activate_playlist_async(self, pid: str) -> bool:
        return self._record("playlist-use", pid)

    def resume_schedule_async(self) -> bool:
        return self._record("schedule-follow")

    def activate_playlist_on_async(self, connector: str, pid: str) -> bool:
        return self._record(connector, "playlist-use", pid)

    def resume_schedule_on_async(self, connector: str) -> bool:
        return self._record(connector, "schedule-follow")

    def runtime_action_on_async(self, connector: str, verb: str, argument: Any = None) -> bool:
        return self._record(connector, verb)

    def reset_display_modes_on_async(self, connector: str) -> bool:
        return self._record(connector, "modes-default")

    def schedule_edited(self) -> None: ...

    def runtime_config_changed(self) -> None: ...

    def window_report(self, _message: str) -> None: ...

    def require_current_library(self) -> None: ...

    def present_page(self, _name: str) -> None: ...

    def authoring_action_async(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def update_settings_async(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


def _classic(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    independent: bool,
    status: Any,
) -> tuple[Any, _App, dict[str, str], Any]:
    """A real classic Schedules page over a real Session; ``status`` may take the ids."""
    session = _session(items)
    pages = _playlist(session, "Pages", items[0])
    scenes = _playlist(session, "Scenes", items[1])
    ids = {"pages": pages, "scenes": scenes}
    if independent:
        from dataclasses import replace

        session.update_settings(
            replace(
                session.settings,
                display_mode=config.DISPLAY_MODE_INDEPENDENT,
                theme_source_connector="DP-1",
            )
        )
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-1",))
    app = _App(session, status(ids) if callable(status) else status)
    page = schedules_page.SchedulesPage(app)  # type: ignore[arg-type]
    page.refresh(session)
    return page, app, ids, session


def _index(session: Any, pid: str) -> int:
    return 1 + [one.id for one in session.playlists.all()].index(pid)


# ============================================ check 3: the splice on a live row ===


def _next_page(backend: Any, items: Any) -> tuple[Shown, dict[str, str]]:  # noqa: F811
    session = backend.session
    ids = {
        "pages": _playlist(session, "Pages", items[0]),
        "scenes": _playlist(session, "Scenes", items[1]),
    }
    return Shown(backend), ids


def _pages_named(ids: dict[str, str]) -> dict[str, Any]:
    """Mirrored, two rows: the pick "Pages" plays (named, and a row on this page)."""
    return _renamed(_mirrored_pick_rows_playing(), {"pages": ids["pages"], "scenes": ids["scenes"]})


def _independent_scenes_named(ids: dict[str, str]) -> dict[str, Any]:
    return _independent_named({"scenes": ids["scenes"], "pages": ids["pages"]})


def _overall(page: Any) -> Adw.ComboRow:
    row = page._playback_row
    assert isinstance(row, Adw.ComboRow)
    return row


def _display(page: Any) -> Adw.ComboRow:
    row = page._display_playback_rows["DP-1"].playlist
    assert isinstance(row, Adw.ComboRow)
    return row


# ================================================================== U-N9 ========


def test_u_n9_classic_overall_named_then_unnamed_says_not_named(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=False, status=_all_media_named
    )
    try:
        assert _shown_item(_overall(page)) == NAMED
        app.runtime_status = _ceiling(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_overall(page)) == UNNAMED
    finally:
        session.shutdown()


def test_u_n9_next_named_then_unnamed_says_not_named(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_all_media_named(ids))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == NAMED
        view.answer(_ceiling(ids))
        assert _shown_item(view.page.play_row) == UNNAMED
    finally:
        view.close()


def test_u_n9_control_classic_display_row_named_then_unnamed_says_not_named(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_all_media
    )
    try:
        assert _shown_item(_display(page)) == NAMED
        app.runtime_status = _independent_ceiling(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_display(page)) == UNNAMED
    finally:
        session.shutdown()


# ================================================================= U-N10 ========


def test_u_n10_a_label_only_change_leaves_no_echo_waiting(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_ceiling
    )
    try:
        assert _shown_item(_display(page)) == UNNAMED
        app.runtime_status = _independent_all_media(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_display(page)) == NAMED
        assert page._display_selected_echo == {}
    finally:
        session.shutdown()


def test_u_n10_control_a_move_onto_the_placeholder_is_consumed(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_scenes_named
    )
    try:
        assert _shown_item(_display(page)) == "Scenes"
        app.runtime_status = _independent_ceiling(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_display(page)) == UNNAMED
        assert page._display_selected_echo == {}
    finally:
        session.shutdown()


# ================================================================= U-N11 ========


def _no_reply_with_an_unlisted_record(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any]:
    session = _session(items)
    _playlist(session, "Pages", items[0])
    gone = _playlist(session, "Gone", items[2])
    session.use_playlist(gone)
    session.playlists.delete(gone)
    assert session.manual_playlist == gone  # the record outlives its playlist
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-1",))
    page = schedules_page.SchedulesPage(_App(session, None))  # type: ignore[arg-type]
    page.refresh(session)
    return page, session


def test_u_n11_no_reply_names_the_apps_record_as_your_pick(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, session = _no_reply_with_an_unlisted_record(items, monkeypatch)
    try:
        assert _shown_item(_overall(page)) == NAMED
    finally:
        session.shutdown()


def test_u_n11_control_a_reply_that_does_not_name_it_still_says_so(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _app, _ids, session = _classic(items, monkeypatch, independent=False, status=_ceiling)
    try:
        assert _shown_item(_overall(page)) == UNNAMED
    finally:
        session.shutdown()


# ============================================================ U-N12: coverage ===
# One test per label clause, each paired with a control that keeps the clause honest.


def test_u_n12_place_playback_shows_a_named_pick_from_a_real_row(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(items, monkeypatch, independent=False, status=_pages_named)
    try:
        assert _shown_item(_overall(page)) == "Pages"
        app.runtime_status = _all_media_named(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_overall(page)) == NAMED
    finally:
        session.shutdown()


def test_u_n12_control_place_playback_leaves_a_real_row_for_an_unnamed_pick(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(items, monkeypatch, independent=False, status=_pages_named)
    try:
        app.runtime_status = _ceiling(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_overall(page)) == "Pages"
    finally:
        session.shutdown()


def test_u_n12_set_display_selected_relabels_the_placeholder(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_ceiling
    )
    try:
        assert _shown_item(_display(page)) == UNNAMED
        app.runtime_status = _independent_all_media(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_display(page)) == NAMED
    finally:
        session.shutdown()


def test_u_n12_control_set_display_selected_keeps_the_label_it_has(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_ceiling
    )
    try:
        app.runtime_status = _independent_ceiling(ids)
        page.runtime_status_changed(session)
        assert _shown_item(_display(page)) == UNNAMED
        assert page._display_selected_echo == {}
    finally:
        session.shutdown()


def test_u_n12_refresh_details_shows_a_named_pick_with_no_row_from_a_real_row(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_pages_named(ids))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == "Pages"
        view.answer(_all_media_named(ids))
        assert _shown_item(view.page.play_row) == NAMED
    finally:
        view.close()


def test_u_n12_control_refresh_details_leaves_a_real_row_for_an_unnamed_pick(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_pages_named(ids))
        view.select("DP-1")
        view.answer(_ceiling(ids))
        assert _shown_item(view.page.play_row) == "Pages"
    finally:
        view.close()


def test_u_n12_next_click_path_labels_the_item_from_the_latest_reply(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_ceiling(ids))
        view.select("DP-1")
        row = view.page.play_row
        assert _shown_item(row) == UNNAMED
        # A reply arrives and the page hasn't refreshed yet: the click path labels it.
        backend.status_model.adopt(_all_media_named(ids))
        row.set_selected(view.page._play_ids.index(ids["pages"]))
        assert _shown_item(row) == NAMED
    finally:
        view.close()


def test_u_n12_control_next_click_path_keeps_the_unnamed_label(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_ceiling(ids))
        view.select("DP-1")
        row = view.page.play_row
        row.set_selected(view.page._play_ids.index(ids["pages"]))
        assert _shown_item(row) == UNNAMED
    finally:
        view.close()


def test_u_n12_build_playback_labels_a_named_pick(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _app, _ids, session = _classic(
        items, monkeypatch, independent=False, status=_all_media_named
    )
    try:
        assert _shown_item(_overall(page)) == NAMED
    finally:
        session.shutdown()


def test_u_n12_control_build_playback_labels_an_unnamed_pick(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _app, _ids, session = _classic(items, monkeypatch, independent=False, status=_ceiling)
    try:
        assert _shown_item(_overall(page)) == UNNAMED
    finally:
        session.shutdown()


# ================================================================ U-N15 ========
# Three label clauses that no test caught: `_pick_label`'s route_unsure, the per-display
# click's label, and the next page's `_pick_label` route_unsure.


def _mirrored_ceiling_split(ids: dict[str, str]) -> dict[str, Any]:
    """Mirrored at the ceiling: DP-1 shows the stand-in, HDMI-A-1 the pick itself.

    The top level is then route_unsure, and not pick_stood_in.
    """
    status = _mirrored_pick_rows()
    second = status["displays"][1]
    second.pop("stand_in_for", None)
    second.update(connector="HDMI-A-1", playlist_id="pages", playlist="pages")
    return _renamed(_at_the_ceiling(status), {"pages": ids["pages"], "scenes": ids["scenes"]})


def test_u_n15_classic_overall_a_mirrored_unsure_pick_is_not_named(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one.ui import runtime_truth

    page, app, ids, session = _classic(
        items, monkeypatch, independent=False, status=_all_media_named
    )
    try:
        status = _mirrored_ceiling_split(ids)
        truth = runtime_truth.from_status(status)
        assert truth is not None and truth.is_manual  # precondition
        assert truth.route_unsure and not truth.pick_stood_in  # precondition
        app.runtime_status = status
        page.runtime_status_changed(session)
        assert _shown_item(_overall(page)) == UNNAMED
    finally:
        session.shutdown()


def test_u_n15_classic_display_click_labels_the_item_from_the_reply(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_all_media
    )
    try:
        row = _display(page)
        assert _shown_item(row) == NAMED
        row.set_selected(_index(session, ids["pages"]))
        assert app.sent == [("DP-1", "playlist-use", ids["pages"])], app.sent
        assert _shown_item(row) == NAMED
    finally:
        session.shutdown()


def test_u_n15_control_classic_display_click_keeps_the_unnamed_label(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _app, ids, session = _classic(
        items, monkeypatch, independent=True, status=_independent_ceiling
    )
    try:
        row = _display(page)
        row.set_selected(_index(session, ids["pages"]))
        assert _shown_item(row) == UNNAMED
    finally:
        session.shutdown()


def test_u_n15_next_page_an_unsure_display_pick_is_not_named(
    backend: Any,  # noqa: F811
    items: Any,  # noqa: F811
) -> None:
    view, ids = _next_page(backend, items)
    try:
        view.answer(_independent_ceiling(ids))
        view.select("DP-1")
        assert _shown_item(view.page.play_row) == UNNAMED
    finally:
        view.close()


# ================================================================ U-L-c ========
# One click on the classic overall "Active playlist" starts one command. The handler's
# own revert is re-emitted by GObject after it returns; it must not start a second.


@pytest.mark.parametrize("answer", [_pages_named, _following_rows])
def test_u_l_c_one_click_on_the_overall_row_sends_one_command(
    answer: Any,
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(items, monkeypatch, independent=False, status=answer)
    try:
        _overall(page).set_selected(_index(session, ids["scenes"]))
        assert app.sent == [("playlist-use", ids["scenes"])], app.sent
    finally:
        session.shutdown()


@pytest.mark.parametrize("answer", [_all_media_named, _ceiling])
def test_u_l_c_control_a_revert_onto_the_item_sends_nothing_more(
    answer: Any,
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(items, monkeypatch, independent=False, status=answer)
    try:
        _overall(page).set_selected(_index(session, ids["scenes"]))
        assert app.sent == [("playlist-use", ids["scenes"])], app.sent
    finally:
        session.shutdown()


def test_u_l_c_a_second_click_after_the_first_is_still_a_click(
    items: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, app, ids, session = _classic(items, monkeypatch, independent=False, status=_pages_named)
    try:
        row = _overall(page)
        row.set_selected(_index(session, ids["scenes"]))
        row.set_selected(_index(session, ids["scenes"]))
        assert app.sent == [("playlist-use", ids["scenes"])] * 2, app.sent
    finally:
        session.shutdown()
