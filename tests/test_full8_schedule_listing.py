"""AE-3 (L-4, L-5): ``ctl schedule`` and compatibility playback share one kept-rules answer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


def _session(tmp_path: Path, rules: list[schedules.Rule]) -> Session:
    picture = MediaItem(path=tmp_path / "a.png", kind=Kind.STILL, size=1, mtime=0)
    authored = {
        "good": playlists.Playlist(
            id="good", name="Good", entries=(playlists.Entry(id="e", source=str(picture.path)),)
        ),
        "empty": playlists.Playlist(id="empty", name="Empty"),
    }
    session = Session(
        config.Settings(roots=(tmp_path,), scan_workshop=False),
        favourite_store=favourites.Store(),
        pairing_store=pairings.Store(),
        playlist_store=playlists.Store(authored),
        schedule_store=schedules.Store(rules),
        display_store=displays.Store(),
        removal_store=removals.Store(),
        clock=lambda: datetime(2026, 10, 17, 12),
        owns_playback=False,
    )
    session.adopt_library(Library(roots=(tmp_path,), items=(picture,)), reconcile_workshop=False)
    return session


def _rows(listing: str) -> dict[str, list[str]]:
    return {
        line.split("\t")[0]: line.split("\t")
        for line in listing.splitlines()
        if not line.startswith("#")
    }


def test_the_kept_rules_are_the_ones_the_compiler_emits(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            schedules.Rule(id="good", playlist="good"),
            schedules.Rule(id="empty", playlist="empty"),
            schedules.Rule(id="missing", playlist="nowhere"),
            schedules.Rule(id="all", playlist=runtime_config.FALLBACK_PLAYLIST_NAME),
        ],
    )
    try:
        built = runtime_config.compiled(session.settings, session)
        assert {left.owner_id for left in built.left_out} == {"empty", "missing"}
        assert [rule.id for rule in session.compiling_schedule_rules()] == ["good", "all"]
    finally:
        session.shutdown()


def test_ctl_schedule_does_not_mark_an_omitted_rule_in_force(tmp_path: Path) -> None:
    """Failed at 2b48044: the listing read the raw rules and marked the omitted one."""
    pytest.importorskip("gi")
    from wall_in_one.ui.app import _Commands

    session = _session(
        tmp_path,
        [schedules.Rule(id="good", playlist="good"), schedules.Rule(id="empty", playlist="empty")],
    )
    try:
        commands = _Commands(cast(Any, SimpleNamespace(session=session)))
        answer = commands.show_schedule()
        rows = _rows(answer.message)
        assert rows["empty"][4] == "no"
        assert rows["good"][4] == "yes"
        assert session.active_playlist() == "good"
    finally:
        session.shutdown()


def test_describe_leaves_everything_in_force_when_nothing_is_omitted() -> None:
    rules = [schedules.Rule(id="a", playlist="x"), schedules.Rule(id="b", playlist="y")]
    rows = _rows(schedules.describe(rules, "", datetime(2026, 10, 17, 12)))
    assert (rows["a"][4], rows["b"][4]) == ("no", "yes")


def test_the_library_is_indexed_only_once_a_matching_rule_names_a_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-minute tick and every classic playlist row ask; a quiet answer must be cheap."""
    session = _session(
        tmp_path,
        [
            schedules.Rule(id="night", playlist="good", start=22 * 60, end=23 * 60),
            schedules.Rule(id="all", playlist=runtime_config.FALLBACK_PLAYLIST_ID, start=1, end=2),
        ],
    )
    calls: list[int] = []
    real = runtime_config.library_index

    def counted(owner: Session) -> dict[Path, MediaItem]:
        calls.append(1)
        return real(owner)

    monkeypatch.setattr(runtime_config, "library_index", counted)
    try:
        assert session.active_playlist() == ""
        assert calls == []
        session.compiling_schedule_rules(datetime(2026, 10, 17, 22, 30))
        assert calls == [1]
    finally:
        session.shutdown()
