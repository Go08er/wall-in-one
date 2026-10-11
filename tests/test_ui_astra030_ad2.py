"""Astra's seventh full review of 0.3.0, AD-2: a still choice cleared elsewhere.

A video with no still of its own plays through a still somebody chose. A
scan folds that choice into the library it hands over
(``MediaItem.paired_still``). Another program then clears the choice. The
guard on removing a playlist's other wallpaper read the pairings afresh but
judged them against that library, where the video still had its still, and
let the playlist be left holding only a video nothing can show.

A library now keeps the still choices its scan folded in
(`Library.chosen_stills`), and the view a guard judges on is scanned from
disk when the saved pairings choose differently (`Session.saved_view`). The
guard then refuses (``the guard asks``; those cases fail on a5999e2). The
scenario is also run as it went before (``the guard doesn't ask``), ending
in a publication: the playlist is left out with the rule that names it, and
that is said. Those cases fail on a5999e2 too, where the compile refused
"playlist 'Evening' has no playable entries".

The accepted library here comes from a real scan with the choice already
saved, not from a ready library given a choice afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_astra030_ac_saved import _verb  # noqa: E402
from tests.test_ui_astra030_ad3 import (  # noqa: E402
    GUARD,
    _as_before_the_fix,
    _as_saved,
    _caught_up,
    _editing,
    _names,
    _published,
)
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    shown,
    toolkit,
)
from wall_in_one import runtime_config  # noqa: E402
from wall_in_one.library import pairings, playlists, scan, workshop  # noqa: E402
from wall_in_one.library.model import Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402

NOTHING_LEFT = "would have nothing left to play"


def _chosen(session: Session) -> list[tuple[str, Path]]:
    """The still choices the session's library was resolved with (`Library.chosen_stills`)."""
    chosen = session.library.chosen_stills
    assert chosen is not None, "the scanner's library says which choices it folded in"
    return list(chosen)


def _a_video_that_plays_through_a_chosen_still(shown: Shown) -> tuple[MediaItem, Path]:  # noqa: F811
    """The Workshop video, with ``dusk.jpg`` chosen for it and a real scan made after that.

    The accepted library is the scanner's, with the choice already folded
    into the video's still: not a ready library given a choice afterwards.
    """
    application = shown.opened.application
    session = application.session
    assert shown.loop.paired_still is None, "the video has no still of its own"
    dusk = shown.walls / "dusk.jpg"
    session.pairings.choose_still(shown.loop, dusk)
    settings = application.settings
    library = scan.scan(
        settings.roots,
        dict(session.pairings.records),
        include_workshop=True,
        workshop_roots=workshop.steam_roots(),
    )
    session.adopt_library(library, reconcile_workshop=False)
    loop = library.find(shown.loop.path)
    assert loop is not None and loop.paired_still == dusk, "the scan folded the choice in"
    assert runtime_config.entry_playability(loop, session).plays
    return loop, dusk


@pytest.mark.parametrize("guard", GUARD)
@pytest.mark.parametrize("cleared", ["its still choice is cleared", "it is reset"])
@pytest.mark.parametrize("via", ["the new window", "ctl"])
def test_ad2_a_choice_cleared_elsewhere_no_longer_plays_the_video_for_the_guard(
    shown: Shown,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    via: str,
    cleared: str,
    guard: str,
) -> None:
    """A playlist a rule uses holds the video and one picture; the picture is removed.

    With the choice cleared the video has nothing to show, so the picture is
    the last thing the playlist plays. The guard read the fresh pairings and
    judged them by the library whose scan had folded the old choice in.
    """
    _as_before_the_fix(monkeypatch, guard)
    opened = shown.opened
    application = opened.application
    session = application.session
    loop, _dusk = _a_video_that_plays_through_a_chosen_still(shown)
    store = session.playlists
    evening = store.create("Evening")
    store.add(evening.id, loop.path)
    picture = store.add(evening.id, shown.dawn.path).entries[1]
    session.schedules.add(evening.id, rule_id="evenings")

    # Another program, finished before the gesture.
    elsewhere = pairings.Store.open()
    if cleared == "it is reset":
        assert elsewhere.reset(loop)
    else:
        elsewhere.choose_still(loop, None)
    held = session.pairings.resolve_accepted(loop, session.library).still
    assert held is not None, "this process still holds the choice, and a library that has it"

    if via == "ctl":
        reply = _verb(shown, "playlist-remove", f"{evening.id} {picture.id}")
        assert reply.ok is (guard == GUARD[1]), reply
        said = reply.message
    else:
        _editing(shown).remove_entries(evening.id, [picture.id])
        opened.wait("the removal", lambda: bool(opened.reports) or guard == GUARD[1])
        said = opened.reports[-1] if opened.reports else ""
    saved = [entry.path for entry in playlists.Store.open().find(evening.id).entries]

    if guard == GUARD[0]:
        # Failed on a5999e2: the picture was removed.
        assert saved == [loop.path, shown.dawn.path], "nothing was removed"
        assert (
            "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play" in said
        )
        assert _as_saved() == []
    else:
        assert saved == [loop.path], "as it went on a5999e2"
        assert len(_as_saved()) == 2, "the rule, and the playlist it names"

    # The application catches up (a scan with the pairings as saved), and publishes.
    # Failed on a5999e2 without the guard: "playlist 'Evening' has no playable entries".
    _caught_up(shown)
    document = _published(shown)
    if guard == GUARD[0]:
        assert "Evening" in _names(document)
        assert [rule["id"] for rule in document["schedules"]] == ["evenings"]
        assert application.left_out_notice == ""
    else:
        assert "Evening" not in _names(document)
        assert "schedules" not in document, "the rule is left out with its playlist"
        assert application.left_out_notice == (
            "A schedule rule names \u201cEvening\u201d, which has nothing to play, so that "
            "rule is left out."
        )
        assert opened.reports.count(application.left_out_notice) == 1, "said, once"


def test_ad2_a_choice_made_in_this_window_does_not_send_every_guard_to_a_scan(
    shown: Shown,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library keeps the choices it was resolved with, also through this app's own.

    A still chosen here is adopted into the library with its choice
    (`Application.adopt_pairing_still`), so the saved pairings still match
    it, and a guarded gesture afterwards judges by the accepted library as
    before, without scanning.
    """
    opened = shown.opened
    application = opened.application
    session = application.session
    loop, dusk = _a_video_that_plays_through_a_chosen_still(shown)
    assert _chosen(session) == [(pairings.Identity.of(loop).key, dusk)]
    scans: list[bool] = []
    real = Session.scanned_from_disk

    def scanned(view: Session) -> Library:
        scans.append(True)
        return real(view)

    monkeypatch.setattr(Session, "scanned_from_disk", scanned)
    evening = session.playlists.create("Evening")
    video = session.playlists.add(evening.id, loop.path).entries[0]
    session.playlists.add(evening.id, shown.dawn.path)

    # Reset here, through the application's own task: the video has no still again.
    done: list[object] = []
    store = session.pairings

    def reset(result: Any) -> None:
        done.append(application.adopt_pairing_still(result.item, result.effective_still))

    application.authoring_action_async(
        lambda: store.reset(loop),
        reset,
        prepare=lambda: application.prepare_pairing_reset(loop),
        failure=done.append,
    )
    opened.wait("the reset", lambda: bool(done))
    adopted = session.library.find(loop.path)
    assert adopted is not None and adopted.paired_still is None, done
    assert _chosen(session) == [], "the library was resolved with no choice now"

    # A removal that is allowed, so it is judged on the worker against what is saved.
    _editing(shown).remove_entries(evening.id, [video.id])
    opened.wait("the removal", lambda: len(playlists.Store.open().find(evening.id).entries) == 1)
    assert opened.reports == []
    assert scans == [], "judged by the accepted library: the saved pairings are the ones it has"
