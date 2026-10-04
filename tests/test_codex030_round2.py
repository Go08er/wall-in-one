"""Codex 0.3.0 round 2, Q-1, without a display: guards that see still dependencies.

A video or scene plays through its representative still, often another
file. Round 2 projected from accepted memory what still the next scan would
give each wallpaper (`scan.projected_still`); round 3 showed the projection
can't keep up with the scanner, so the guarantee is now a dry run of the
real scan, cleanup and compiler (`wall_in_one.dry_run`), and round 2's
projection conformance became the dry run's (``test_dry_run``, the "tree"
fixtures). These are Codex's round-2 cases through the dry run, over real
files; the application's own scenarios are in ``test_ui_codex030_round2``.
Each named for Codex's evidence fails on 8ce588d.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, dry_run, runtime_config
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session


def _file(path: Path, body: bytes = b"placeholder bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    made = Session(
        config.Settings(roots=(tmp_path / "A", tmp_path / "B")).validated(),
        owns_playback=False,
    )
    try:
        yield made
    finally:
        made.shutdown()


def _scheduled(session: Session, name: str, sources: list[Path]) -> None:
    made = session.playlists.create(name)
    for source in sources:
        session.playlists.add(made.id, source)
    session.schedules.add(made.id)


def _removing(session: Session, path: Path) -> tuple[str, ...]:
    item = session.library.find(path)
    assert item is not None
    return dry_run.removal(
        item,
        settings=session.settings,
        library=session.library,
        roots=session.library.roots,
        adopted_authority=None,
        favourite_store=session.favourites,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        uses=dry_run.Uses.of(session),
    ).refusals


def test_remove_guard_ignores_representative_dependency(tmp_path: Path, session: Session) -> None:
    """Codex Q-1 A: deleting the image a video plays through, with no other still for it."""
    a = tmp_path / "A"
    video = _file(a / "rain.mp4")
    still = _file(a / "forest.png")
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    session.refresh(mutate_removals=False)
    found = session.library.find(video)
    assert found is not None and found.paired_still == still
    _scheduled(session, "Evening", [video])
    runtime_config.render(session.settings, session)  # buildable now

    assert _removing(session, still) == (
        "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play: "
        "rain.mp4 plays through forest.png, which this removes",
    )
    # The gesture's early check sees only what leaves the library; the dry run is the guarantee.
    assert runtime_config.emptied_by_removing([still], session.settings, session) == []
    # With a sibling to fall back on, the video keeps playing: allowed.
    _file(a / "rain-still.png")
    session.refresh(mutate_removals=False)
    assert _removing(session, still) == ()


def test_a_chosen_still_in_a_folder_no_longer_scanned_stays_and_the_change_is_allowed(
    tmp_path: Path, session: Session
) -> None:
    """Codex's allowed case: the scan attaches a chosen still wherever it is."""
    a, b = tmp_path / "A", tmp_path / "B"
    still = _file(a / "forest.png")
    video = _file(b / "rain.mp4")
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    session.refresh(mutate_removals=False)
    _scheduled(session, "Evening", [video])

    verdict = dry_run.sources(
        session.settings,
        replace(session.settings, roots=(b,)).validated(),
        library=session.library,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        favourite_store=session.favourites,
        uses=dry_run.Uses.of(session),
    )
    assert verdict.refusals == ()
    after = verdict.library.find(video)
    assert after is not None and after.paired_still == still, "the real scan agrees"
