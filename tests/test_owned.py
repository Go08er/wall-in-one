"""Recognising a browse result the library already holds."""

from __future__ import annotations

import json
from pathlib import Path

from wall_in_one.library import owned
from wall_in_one.library.model import Kind
from wall_in_one.providers.base import WallpaperCandidate


def _candidate(
    provider: str = "wallhaven",
    identifier: str = "abc123",
    page_url: str = "https://wallhaven.cc/w/abc123",
) -> WallpaperCandidate:
    return WallpaperCandidate(
        provider=provider,
        identifier=identifier,
        title="A wallpaper",
        kind=Kind.STILL,
        page_url=page_url,
    )


def _install(
    directory: Path,
    name: str,
    suffix: str,
    payload: dict[str, object],
    *,
    media: bool = True,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if media:
        target.write_bytes(b"not really an image")
    (directory / f"{name}{suffix}").write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_a_wallhaven_download_is_recognised_by_its_id(tmp_path: Path) -> None:
    installed = _install(
        tmp_path / "Wall-in-One" / "Wallhaven",
        "abc123.jpg",
        ".wallhaven.json",
        {"provider": "Wallhaven", "id": "abc123", "source_page": "https://wallhaven.cc/w/abc123"},
    )

    index = owned.read([tmp_path])

    assert index.path_for(_candidate()) == installed
    assert index.holds(_candidate())
    assert len(index) == 1


def test_the_sidecars_provider_casing_does_not_matter(tmp_path: Path) -> None:
    """The sidecar says "Wallhaven"; the provider calls itself "wallhaven"."""
    _install(
        tmp_path / "Wallhaven",
        "abc123.jpg",
        ".wallhaven.json",
        {"provider": "Wallhaven", "id": "abc123"},
    )
    assert owned.read([tmp_path]).holds(_candidate())


def test_an_old_motionbgs_sidecar_matches_on_its_page(tmp_path: Path) -> None:
    """Sidecars written before `id` was recorded still have to be recognised.

    MotionBGS never wrote one, so every download that predates this module is
    matchable only by its source page. Dropping them would mean the browser
    forgetting everything the user had already downloaded.
    """
    installed = _install(
        tmp_path / "MotionBGS",
        "misty-forest.mp4",
        ".motionbgs.json",
        {"provider": "MotionBGS", "source_page": "https://motionbgs.com/misty-forest/"},
    )

    index = owned.read([tmp_path])
    candidate = _candidate(
        provider="motionbgs",
        identifier="misty-forest",
        page_url="https://motionbgs.com/misty-forest/",
    )
    assert index.path_for(candidate) == installed


def test_a_sidecar_whose_file_is_gone_is_not_held(tmp_path: Path) -> None:
    """Removing a wallpaper leaves its sidecar behind if the unlink half-failed.

    Claiming to hold a file that is not there is the one wrong answer here: it
    hides the result and offers no way to get it back.
    """
    _install(
        tmp_path / "Wallhaven",
        "abc123.jpg",
        ".wallhaven.json",
        {"provider": "Wallhaven", "id": "abc123"},
        media=False,
    )
    assert not owned.read([tmp_path]).holds(_candidate())


def test_a_malformed_sidecar_does_not_lose_the_others(tmp_path: Path) -> None:
    directory = tmp_path / "Wallhaven"
    directory.mkdir(parents=True)
    (directory / "broken.jpg").write_bytes(b"x")
    (directory / "broken.jpg.wallhaven.json").write_text("{not json", encoding="utf-8")
    _install(directory, "abc123.jpg", ".wallhaven.json", {"provider": "Wallhaven", "id": "abc123"})

    index = owned.read([tmp_path])

    assert index.holds(_candidate())
    assert len(index) == 1


def test_an_unrelated_result_is_not_held(tmp_path: Path) -> None:
    _install(
        tmp_path / "Wallhaven",
        "abc123.jpg",
        ".wallhaven.json",
        {"provider": "Wallhaven", "id": "abc123"},
    )
    assert not owned.read([tmp_path]).holds(_candidate(identifier="zzz999", page_url=""))


def test_a_pairing_sidecar_is_not_a_download_record(tmp_path: Path) -> None:
    """Only the two provider suffixes say where a file came from."""
    directory = tmp_path / "stills"
    directory.mkdir(parents=True)
    (directory / "shot.png").write_bytes(b"x")
    (directory / "shot.png.wall-in-one.json").write_text('{"provider": "Wallhaven"}', "utf-8")

    assert len(owned.read([tmp_path])) == 0


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    assert len(owned.read([tmp_path / "nowhere"])) == 0


def test_a_finished_download_is_recorded_without_rescanning(tmp_path: Path) -> None:
    index = owned.Index()
    candidate = _candidate()
    assert not index.holds(candidate)

    index.add(candidate, tmp_path / "abc123.jpg")

    assert index.path_for(candidate) == tmp_path / "abc123.jpg"


def test_overlapping_roots_count_a_download_once(tmp_path: Path) -> None:
    """The settings allow a root inside another root."""
    _install(
        tmp_path / "outer" / "inner",
        "abc123.jpg",
        ".wallhaven.json",
        {"provider": "Wallhaven", "id": "abc123"},
    )
    assert len(owned.read([tmp_path / "outer", tmp_path / "outer" / "inner"])) == 1
