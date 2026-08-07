"""The on-disk side of the thumbnail cache: validity, bounds, and eviction.

No ffmpeg and no GTK here on purpose. Entries are written directly, which is
what makes the awkward cases -- a truncated entry, a stranger's file, a symlink
pointing out of the directory -- expressible at all.

Every test points `XDG_CACHE_HOME` at `tmp_path`, so nothing in this file can
see, let alone delete, anything in the user's real cache.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from pathlib import Path

import pytest

from wall_in_one import thumbnails
from wall_in_one.library.model import Kind, MediaItem


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The thumbnail directory, under a sandboxed `XDG_CACHE_HOME`."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = thumbnails.cache_directory()
    directory.mkdir(parents=True)
    return directory


def _png(payload: bytes = b"\x00\x00") -> bytes:
    """A real, complete, 1x1 PNG -- built rather than pasted in as a blob."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        block = kind + body
        return struct.pack(">I", len(body)) + block + struct.pack(">I", zlib.crc32(block))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(payload))
        + chunk(b"IEND", b"")
    )


def _entry(cache: Path, index: int, *, size: int = 1024, age: float = 0.0) -> Path:
    """One cache entry of a given weight, last used ``age`` seconds ago."""
    path = cache / f"{index:032x}.png"
    body = _png()
    path.write_bytes(body + b"\x00" * max(0, size - len(body)))
    used_at = time.time() - age
    os.utime(path, (used_at, used_at))
    return path


def _item(path: Path, *, size: int = 1, mtime: int = 0) -> MediaItem:
    return MediaItem(path=path, kind=Kind.STILL, size=size, mtime=mtime)


def _total(cache: Path) -> int:
    return sum(path.stat().st_size for path in cache.iterdir() if path.is_file())


# -- keys ----------------------------------------------------------------


def test_a_wallpaper_edited_in_place_gets_a_different_entry(cache: Path) -> None:
    """Same path, new contents: mtime and size are in the key so this misses."""
    before = thumbnails.cached_path(_item(Path("/w/a.png"), size=100, mtime=1))
    after = thumbnails.cached_path(_item(Path("/w/a.png"), size=100, mtime=2))
    resized = thumbnails.cached_path(_item(Path("/w/a.png"), size=200, mtime=1))
    assert before != after
    assert before != resized


def test_the_tile_geometry_is_part_of_the_key(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the tile size must not leave stale thumbnails at the old one."""
    item = _item(Path("/w/a.png"))
    before = thumbnails.cache_key(item)
    monkeypatch.setattr(thumbnails, "THUMBNAIL_WIDTH", thumbnails.THUMBNAIL_WIDTH * 2)
    assert thumbnails.cache_key(item) != before


def test_an_entry_name_is_shaped_the_way_pruning_expects(cache: Path) -> None:
    """The key format and the deletion whitelist have to agree, or nothing is
    ever evicted."""
    name = thumbnails.cached_path(_item(Path("/w/a.png"))).name
    assert thumbnails._ENTRY_NAME.match(name)


# -- what counts as a hit ------------------------------------------------


def test_a_complete_entry_is_a_hit(cache: Path) -> None:
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).write_bytes(_png())
    assert thumbnails.lookup(item) == thumbnails.cached_path(item)


def test_nothing_cached_is_a_miss(cache: Path) -> None:
    assert thumbnails.lookup(_item(Path("/w/a.png"))) is None


def test_a_truncated_entry_is_a_miss(cache: Path) -> None:
    """The half-written file a power cut leaves behind must not reach a decoder."""
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).write_bytes(_png()[:-9])
    assert thumbnails.lookup(item) is None


def test_an_entry_that_is_not_a_png_at_all_is_a_miss(cache: Path) -> None:
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).write_bytes(b"<html>certainly not a thumbnail</html>")
    assert thumbnails.lookup(item) is None


def test_an_empty_entry_is_a_miss(cache: Path) -> None:
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).touch()
    assert thumbnails.lookup(item) is None


def test_a_directory_where_an_entry_should_be_is_a_miss(cache: Path) -> None:
    """A crash is not the only way something odd ends up in a cache directory."""
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).mkdir()
    assert thumbnails.lookup(item) is None


def test_an_entry_that_is_a_symlink_is_never_read_through(cache: Path, tmp_path: Path) -> None:
    """Even pointing at a perfectly good PNG: we read what we wrote, or nothing."""
    elsewhere = tmp_path / "outside.png"
    elsewhere.write_bytes(_png())
    item = _item(Path("/w/a.png"))
    thumbnails.cached_path(item).symlink_to(elsewhere)

    assert thumbnails.lookup(item) is None


def test_a_hit_marks_the_entry_used(cache: Path) -> None:
    """Eviction order is only meaningful if reading counts as using."""
    item = _item(Path("/w/a.png"))
    path = thumbnails.cached_path(item)
    path.write_bytes(_png())
    stale = time.time() - 10 * thumbnails.TOUCH_INTERVAL_SECONDS
    os.utime(path, (stale, stale))

    assert thumbnails.lookup(item) == path
    assert path.stat().st_mtime > stale


def test_a_recent_hit_does_not_rewrite_the_timestamp(cache: Path) -> None:
    """A grid rebuild must not cost one write syscall per visible tile."""
    item = _item(Path("/w/a.png"))
    path = thumbnails.cached_path(item)
    path.write_bytes(_png())
    stamp = path.stat().st_mtime_ns

    thumbnails.lookup(item)

    assert path.stat().st_mtime_ns == stamp


# -- eviction ------------------------------------------------------------


def test_eviction_drops_the_least_recently_used_first(cache: Path) -> None:
    for index in range(5):
        _entry(cache, index, size=1000, age=10_000 - index * 100)

    thumbnails.prune(max_bytes=2500)

    survivors = sorted(path.name for path in cache.iterdir())
    assert survivors == [f"{3:032x}.png", f"{4:032x}.png"]


def test_eviction_follows_use_and_not_creation_order(cache: Path) -> None:
    """The oldest entry survives because something read it a moment ago."""
    for index in range(4):
        _entry(cache, index, size=1000, age=10_000 - index * 100)
    revived = cache / f"{0:032x}.png"
    recent = time.time() - 200
    os.utime(revived, (recent, recent))

    thumbnails.prune(max_bytes=2000)

    assert revived.exists()
    assert not (cache / f"{1:032x}.png").exists()


def test_eviction_gets_the_cache_under_the_ceiling(cache: Path) -> None:
    for index in range(20):
        _entry(cache, index, size=1000, age=10_000 - index)

    thumbnails.prune(max_bytes=5000)

    assert _total(cache) <= 5000


def test_eviction_leaves_headroom_below_the_ceiling(cache: Path) -> None:
    """Trimming to the ceiling exactly would mean an unlink per thumbnail after."""
    for index in range(20):
        _entry(cache, index, size=1000, age=10_000 - index)

    thumbnails.prune(max_bytes=10_000)

    assert _total(cache) <= int(10_000 * thumbnails.PRUNE_TARGET_RATIO)


def test_a_cache_under_the_ceiling_is_left_alone(cache: Path) -> None:
    for index in range(3):
        _entry(cache, index, size=1000, age=10_000)

    assert thumbnails.prune(max_bytes=100_000) == 0
    assert len(list(cache.iterdir())) == 3


def test_an_entry_in_use_survives_even_over_the_ceiling(cache: Path) -> None:
    """A second instance may have written this one seconds ago to draw it now."""
    fresh = _entry(cache, 0, size=10_000, age=1.0)

    thumbnails.prune(max_bytes=100)

    assert fresh.exists()


def test_pruning_a_cache_that_does_not_exist_is_harmless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "nothing-here"))
    assert thumbnails.prune() == 0
    assert not thumbnails.cache_directory().exists()


def test_the_default_ceiling_is_a_real_bound(cache: Path) -> None:
    """The point of the whole exercise: unlike the plugin this replaces, there
    is a number, and it is finite."""
    assert 0 < thumbnails.MAX_CACHE_BYTES < 1024**3


# -- what eviction refuses to touch --------------------------------------


def test_a_strangers_file_is_neither_counted_nor_deleted(cache: Path) -> None:
    """We delete files shaped like the ones we write, and nothing else."""
    foreign = cache / "notes.txt"
    foreign.write_bytes(b"y" * 50_000)
    for index in range(4):
        _entry(cache, index, size=1000, age=10_000 - index)

    thumbnails.prune(max_bytes=2000)

    assert foreign.read_bytes() == b"y" * 50_000


def test_a_subdirectory_is_left_where_it_is(cache: Path) -> None:
    intruder = cache / "somedir"
    intruder.mkdir()
    thumbnails.prune(max_bytes=0)
    assert intruder.is_dir()


def test_eviction_never_reaches_outside_the_cache_directory(cache: Path, tmp_path: Path) -> None:
    """A symlink named like an entry must not become a way to delete its target."""
    precious = tmp_path / "precious.png"
    precious.write_bytes(_png())
    (cache / f"{0:032x}.png").symlink_to(precious)
    _entry(cache, 1, size=10_000, age=10_000)

    thumbnails.prune(max_bytes=0)

    assert precious.exists()


def test_a_stale_temporary_is_swept(cache: Path) -> None:
    """What a killed encode leaves behind; nothing else writes these."""
    abandoned = cache / f".{0:032x}.4242.tmp.png"
    abandoned.write_bytes(b"half a frame")
    old = time.time() - 2 * thumbnails.TEMPORARY_GRACE_SECONDS
    os.utime(abandoned, (old, old))

    assert thumbnails.prune() == 1
    assert not abandoned.exists()


def test_a_temporary_being_written_right_now_is_left_alone(cache: Path) -> None:
    live = cache / f".{0:032x}.4242.tmp.png"
    live.write_bytes(b"half a frame")

    assert thumbnails.prune() == 0
    assert live.exists()


# -- clearing and reporting ----------------------------------------------


def test_clear_removes_every_thumbnail_we_wrote(cache: Path) -> None:
    for index in range(4):
        _entry(cache, index)
    temporary = cache / f".{9:032x}.7.tmp.png"
    temporary.write_bytes(b"x")

    assert thumbnails.clear() == 5
    assert list(cache.iterdir()) == []


def test_clear_still_leaves_a_strangers_file_alone(cache: Path) -> None:
    foreign = cache / "README"
    foreign.write_bytes(b"not ours")
    _entry(cache, 0)

    thumbnails.clear()

    assert foreign.exists()


def test_clear_on_an_empty_cache_reports_nothing(cache: Path) -> None:
    assert thumbnails.clear() == 0


def test_usage_reports_what_the_cache_costs(cache: Path) -> None:
    """A Settings button wants to say how much it is about to free."""
    for index in range(3):
        _entry(cache, index, size=2000)
    (cache / "unrelated.bin").write_bytes(b"z" * 9999)

    reported = thumbnails.usage()

    assert reported.entries == 3
    assert reported.total_bytes == 6000
