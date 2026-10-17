"""AE-3: Python compatibility playback follows the compiler's kept calendar.

No renderer is called: the cursor and its rotation show what the legacy
service's timers and navigation would apply when no Rust runtime is running.
All stores and the accepted library are explicit private fixtures.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


@pytest.fixture(params=("missing", "empty"))
def bad_reference(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=(False, True), ids=("default", "earlier rule"))
def earlier_rule(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(params=(False, True), ids=("default id", "default name"))
def default_by_name(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture
def case(
    tmp_path: Path, bad_reference: str, earlier_rule: bool, default_by_name: bool
) -> Iterator[tuple[Session, list[datetime], str, Path]]:
    items = tuple(
        MediaItem(path=tmp_path / f"{name}.png", kind=Kind.STILL, size=1, mtime=0)
        for name in ("default", "earlier", "outside")
    )
    authored = {
        identifier: playlists.Playlist(
            id=identifier,
            name=name,
            entries=(playlists.Entry(id=f"entry-{identifier}", source=str(item.path)),),
        )
        for identifier, name, item in zip(
            ("default", "earlier", "manual"), ("Default", "Earlier", "Manual"), items, strict=True
        )
    }
    authored["empty"] = playlists.Playlist(id="empty", name="Empty")
    rules = ([schedules.Rule(id="earlier", playlist="earlier")] if earlier_rule else []) + [
        schedules.Rule(id="left-out", playlist=bad_reference, start=18 * 60, end=23 * 60)
    ]
    now = [datetime(2026, 10, 17, 12)]
    session = Session(
        config.Settings(
            roots=(tmp_path,),
            scan_workshop=False,
            active_playlist="Default" if default_by_name else "default",
        ),
        favourite_store=favourites.Store(path=tmp_path / "favourites.json"),
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
        playlist_store=playlists.Store(authored, path=tmp_path / "playlists.json"),
        schedule_store=schedules.Store(rules, path=tmp_path / "schedules.json"),
        display_store=displays.Store(path=tmp_path / "displays.json"),
        removal_store=removals.Store(path=tmp_path / "pending-removals.json"),
        clock=lambda: now[0],
        owns_playback=False,
    )
    session.adopt_library(Library(roots=(tmp_path,), items=items), reconcile_workshop=False)
    expected = "earlier" if earlier_rule else "default"
    try:
        yield session, now, expected, items[1 if earlier_rule else 0].path
    finally:
        session.shutdown()


def _assert_follows_compiler(session: Session, expected: str, expected_path: Path) -> None:
    built = runtime_config.compiled(session.settings, session)
    document = tomllib.loads(built.document)
    assert [left.owner_id for left in built.left_out] == ["left-out"]
    emitted_rules = document.get("schedules", [])
    selected = emitted_rules[-1]["playlist"] if emitted_rules else document["default_playlist"]
    assert selected == expected
    assert session.active_playlist() == selected
    assert tuple(item.path for item in session.playlist.items) == (expected_path,)
    assert session.cursor is not None and session.cursor.path == expected_path


def test_first_selection_uses_the_kept_calendar(
    case: tuple[Session, list[datetime], str, Path],
) -> None:
    session, now, expected, expected_path = case
    now[0] = now[0].replace(hour=21)
    session.playlists_changed()
    _assert_follows_compiler(session, expected, expected_path)


def test_resume_uses_the_kept_calendar(
    case: tuple[Session, list[datetime], str, Path],
) -> None:
    session, now, expected, expected_path = case
    now[0] = now[0].replace(hour=21)
    session.use_playlist("manual")
    before_resume = session.manual_playlist
    assert before_resume == "manual"
    session.resume_schedule()
    assert session.manual_playlist is None
    _assert_follows_compiler(session, expected, expected_path)


def test_an_omitted_rule_starting_does_not_change_the_rotation(
    case: tuple[Session, list[datetime], str, Path],
) -> None:
    session, now, expected, expected_path = case
    _assert_follows_compiler(session, expected, expected_path)
    now[0] = now[0].replace(hour=21)
    assert not session.schedule_changed()
    _assert_follows_compiler(session, expected, expected_path)


def test_an_emitted_borked_rule_still_wins_but_a_disabled_rule_does_not(
    case: tuple[Session, list[datetime], str, Path],
) -> None:
    session, now, _expected, _expected_path = case
    now[0] = now[0].replace(hour=21)
    item = session.library.items[-1]
    session.pairings.mark_borked(item, "decoder refused this wallpaper", "renderer")
    session.schedules.add("manual", rule_id="borked")
    session.schedules.add("default", rule_id="disabled", enabled=False)
    session.playlists_changed()

    document = tomllib.loads(runtime_config.compiled(session.settings, session).document)
    assert (
        next(rule for rule in document["schedules"] if rule["id"] == "borked")["playlist"]
        == "manual"
    )
    assert session.active_playlist() == "manual"
    assert tuple(entry.path for entry in session.playlist.items) == (item.path,)


def test_repeated_rules_check_each_playlist_once_and_stop_at_its_first_compilable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = MediaItem(path=tmp_path / "first.png", kind=Kind.STILL, size=1, mtime=0)
    repeated = playlists.Playlist(
        id="repeated",
        name="Repeated",
        entries=(
            playlists.Entry(id="first", source=str(item.path)),
            *(
                playlists.Entry(id=f"absent-{index}", source=str(tmp_path / f"absent-{index}.png"))
                for index in range(64)
            ),
        ),
    )
    rules = tuple(
        schedules.Rule(id=f"rule-{index}", playlist="repeated" if index % 2 else "Repeated")
        for index in range(32)
    )
    session = Session(
        config.Settings(roots=(tmp_path,), scan_workshop=False),
        favourite_store=favourites.Store(path=tmp_path / "favourites.json"),
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
        playlist_store=playlists.Store({repeated.id: repeated}, path=tmp_path / "playlists.json"),
        schedule_store=schedules.Store(rules, path=tmp_path / "schedules.json"),
        display_store=displays.Store(path=tmp_path / "displays.json"),
        removal_store=removals.Store(path=tmp_path / "pending-removals.json"),
        owns_playback=False,
    )
    calls: list[MediaItem | None] = []
    original = runtime_config.entry_playability

    def counted(found: MediaItem | None, view: Session) -> runtime_config.Playability:
        calls.append(found)
        return original(found, view)

    try:
        session.adopt_library(Library(roots=(tmp_path,), items=(item,)), reconcile_workshop=False)
        monkeypatch.setattr(runtime_config, "entry_playability", counted)
        assert session.active_playlist() == repeated.id
        assert calls == [item], "one playlist, one first usable entry, for this call"
    finally:
        session.shutdown()
