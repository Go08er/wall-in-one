"""Codex 0.3.0 round 2, Q-1, without a display: guards that see still dependencies.

A video or scene plays through its representative still, often another
file. The guards now ask, from accepted memory, what still the next scan
would give each wallpaper that stays (`scan.projected_still`), and the
compiler's rule decides on that projection (`runtime_config.still_losses`).
These tests run the real scan over real files, so the projection is held to
what the scan does; the application's own scenarios are in
``test_ui_codex030_round2``. Each named for Codex's evidence fails on 8ce588d.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, pairing, playlists, scan, schedules, workshop
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.pairings import Identity, Pairing
from wall_in_one.session import Session


def _file(path: Path, body: bytes = b"placeholder bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _capture(root: Path, name: str) -> Path:
    return _file(pairing.still_directory(root) / f"{name}.png")


def _scene(steam: Path, identifier: str) -> Path:
    """An installed Wallpaper Engine scene, as Steam lays one out."""
    directory = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / identifier
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Field", "file": "scene.json"}), encoding="utf-8"
    )
    (directory / "scene.pkg").write_bytes(b"\x00" * 16)
    return directory


def _scan(
    roots: tuple[Path, ...], records: dict[str, Pairing], steam: Path | None = None
) -> Library:
    return scan.scan(
        roots,
        records,
        include_workshop=steam is not None,
        workshop_roots=(steam,) if steam is not None else None,
        adoption_candidates=(),
    )


def _chosen(video: Path, still: Path | None) -> tuple[str, Pairing]:
    identity = Identity.of(MediaItem(video, Kind.VIDEO, 1, 1))
    return identity.key, Pairing(identity=identity, still=still, customized=True)


# -- the projection is what the scan does ---------------------------------------------------


class Tree(NamedTuple):
    a: Path
    b: Path
    steam: Path
    records: dict[str, Pairing]
    chosen_in_a: Path


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    """Two folders, a Steam library, and every way a wallpaper gets its still."""
    a, b, steam = tmp_path / "A", tmp_path / "B", tmp_path / "steam"
    chosen_in_a = _file(a / "forest.png")
    v1 = _file(b / "v1.mp4")  # chosen still in the other folder
    _file(a / "v2.mp4")  # sibling
    _file(a / "v2-still.png")
    v3 = _file(b / "v3.mp4")  # capture under A only
    _capture(a, pairing.automatic_still_stem(v3))
    v4 = _file(b / "v4.mp4")  # captures under both: the first root's wins
    _capture(a, pairing.automatic_still_stem(v4))
    _capture(b, pairing.automatic_still_stem(v4))
    v5 = _file(b / "v5.mp4")  # sidecar naming a picture under A
    side = _file(a / "side.png")
    v5.with_name(v5.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({pairing.SIDECAR_STILL_KEY: str(side)}), encoding="utf-8"
    )
    v6 = _file(b / "v6.mp4")  # chosen still missing: the capture under B instead
    _capture(b, pairing.automatic_still_stem(v6))
    _file(b / "v7.mp4")  # nothing at all
    _scene(steam, "123")
    _capture(a, "123")  # the scene's capture, under A
    records = dict((_chosen(v1, chosen_in_a), _chosen(v6, tmp_path / "gone.png")))
    return Tree(a, b, steam, records, chosen_in_a)


def test_the_projected_still_is_what_the_scan_gives_for_every_set_of_roots(tree: Tree) -> None:
    accepted = _scan((tree.a, tree.b), tree.records, tree.steam)
    moving = [item for item in accepted.items if item.is_moving]
    assert len(moving) == 8, "seven videos and a scene"
    for roots in ((tree.a, tree.b), (tree.b, tree.a), (tree.b,), (tree.a,)):
        real = _scan(roots, tree.records, tree.steam)
        for item in moving:
            found = real.find(item.path)
            if found is None:
                continue  # it left the library: `would_find`'s question
            record = tree.records.get(Identity.of(item).key)
            projected = scan.projected_still(
                item, accepted, record.still if record is not None else None, roots
            )
            assert projected == found.paired_still, (item.path.name, roots)


def test_the_projected_still_after_a_deletion_is_what_the_next_scan_gives(tree: Tree) -> None:
    accepted = _scan((tree.a, tree.b), tree.records, tree.steam)
    roots = (tree.a, tree.b)
    # The chosen still deleted, and the removal's cleanup clears the choice.
    tree.chosen_in_a.unlink()
    cleared = {
        key: replace(record, still=None) if record.still == tree.chosen_in_a else record
        for key, record in tree.records.items()
    }
    real = _scan(roots, cleared, tree.steam)
    for item in accepted.items:
        if not item.is_moving:
            continue
        found = real.find(item.path)
        assert found is not None
        record = cleared.get(Identity.of(item).key)
        projected = scan.projected_still(
            item,
            accepted,
            record.still if record is not None else None,
            roots,
            removed={tree.chosen_in_a},
        )
        assert projected == found.paired_still, item.path.name


# -- the guards ------------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    made = Session(
        config.Settings(roots=(tmp_path / "A", tmp_path / "B"), scan_workshop=True).validated(),
        playlist_store=playlists.Store(path=tmp_path / "playlists.json"),
        schedule_store=schedules.Store(path=tmp_path / "schedules.json"),
        display_store=displays.Store(path=tmp_path / "displays.json"),
    )
    try:
        yield made
    finally:
        made.shutdown()


def _scheduled(session: Session, name: str, sources: list[Path]) -> playlists.Playlist:
    made = session.playlists.create(name)
    for source in sources:
        session.playlists.add(made.id, source)
    session.schedules.add(made.id)
    return session.playlists.find(made.id)


def test_remove_guard_ignores_representative_dependency(tmp_path: Path, session: Session) -> None:
    """Codex Q-1 A: deleting the image a video plays through, with no other still for it."""
    a = tmp_path / "A"
    video = _file(a / "rain.mp4")
    still = _file(a / "forest.png")
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    session.adopt_library(_scan((a,), dict(session.pairings.records)), reconcile_workshop=False)
    found = session.library.find(video)
    assert found is not None and found.paired_still == still
    _scheduled(session, "Evening", [video])
    runtime_config.render(session.settings, session)  # buildable now

    assert runtime_config.emptied_by_removing([still], session.settings, session) == [
        "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play"
    ]
    # With a sibling to fall back on, the video keeps playing: allowed.
    _file(a / "rain-still.png")
    session.adopt_library(_scan((a,), dict(session.pairings.records)), reconcile_workshop=False)
    assert runtime_config.emptied_by_removing([still], session.settings, session) == []


def test_a_chosen_still_in_a_folder_no_longer_scanned_stays_and_the_change_is_allowed(
    tmp_path: Path, session: Session
) -> None:
    """Codex's allowed case: the scan attaches a chosen still wherever it is."""
    a, b = tmp_path / "A", tmp_path / "B"
    still = _file(a / "forest.png")
    video = _file(b / "rain.mp4")
    session.pairings.choose_still(MediaItem(video, Kind.VIDEO, 1, 1), still)
    records = dict(session.pairings.records)
    session.adopt_library(_scan((a, b), records), reconcile_workshop=False)
    _scheduled(session, "Evening", [video])

    assert runtime_config.settings_refusal({"roots": (b,)}, session) == ""
    after = _scan((b,), records).find(video)
    assert after is not None and after.paired_still == still, "the real scan agrees"
