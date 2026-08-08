"""Thumbnail cache.

Everything goes through ffmpeg, stills included. That looks like overkill for a
PNG until you notice this closure's GdkPixbuf has no webp or avif loader, while
`IMAGE_EXTENSIONS` has both -- and that videos need a frame grab regardless. One
tool that handles every format we accept beats branching on which pixbuf loaders
happen to be installed.

Measured at ~0.3s per thumbnail, including seeking a 4K mp4, so generation
belongs off the main thread. See `wall_in_one.ui.thumbnails` for that part.

That cost is also why the cache is on disk rather than in memory: a library the
user has not changed should cost nothing to show on the second launch. A disk
cache then has to answer three questions the in-memory one never did -- when an
entry stops being valid, how large the directory may grow, and what happens when
something in it is not what we wrote. The answers are, in order: the key carries
the source file's size and mtime, so editing a wallpaper in place misses; the
directory is bounded by :data:`MAX_CACHE_BYTES` and evicted least-recently-used
first; and every read is validated, so a truncated or foreign file is a miss
rather than a decode failure.

This module deliberately knows nothing about GTK, which is what makes all of the
above testable without a display.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.library.model import Kind, MediaItem

#: Tile geometry. 16:9 because that is what most wallpapers are, and a grid of
#: mismatched aspect ratios reads as noise.
THUMBNAIL_WIDTH: Final = 320
THUMBNAIL_HEIGHT: Final = 180

#: Seek this far in before grabbing a video frame. Frame zero is very often
#: black or a fade-in, which makes for a useless thumbnail.
VIDEO_SEEK_SECONDS: Final = 1.0

#: A wedged decode must not hang the worker forever.
GENERATE_TIMEOUT: Final = 20.0

#: Bytes the thumbnail directory may occupy. A 320x180 PNG of photographic
#: content measures 60-120 KB, so this holds on the order of 2,500 thumbnails --
#: comfortably past what a library scan, itself capped at 4096 wallpapers, will
#: usually produce, and still an unremarkable amount of space to find in
#: `~/.cache`. The exact number matters far less than there being one: the
#: plugin this app replaces had no bound at all and no way to clear what it
#: left behind.
MAX_CACHE_BYTES: Final = 256 * 1024 * 1024

#: Ceiling on one cached remote preview. A card thumbnail is tens of KB and a
#: detail preview is single-digit MB; past this it is not a preview and does
#: not belong in a cache sized for thousands of them.
MAX_PREVIEW_ENTRY_BYTES: Final = 16 * 1024 * 1024

#: Evict down to this fraction of the ceiling rather than exactly to it. Trimming
#: to the ceiling exactly would mean an unlink for every thumbnail generated
#: thereafter; leaving headroom makes eviction happen in occasional batches.
PRUNE_TARGET_RATIO: Final = 0.9

#: Never evict an entry stamped more recently than this. Unlinking a file another
#: process is reading is harmless on POSIX -- its descriptor keeps the inode --
#: and a reader that loses the race just treats the entry as a miss. The grace
#: window is for the narrower case of a second instance that has just written a
#: thumbnail it is about to display, which would otherwise be worth regenerating.
EVICTION_GRACE_SECONDS: Final = 60.0

#: How long an abandoned temporary may sit before it is swept. Longer than
#: `GENERATE_TIMEOUT` by a wide margin, so a slow encode in another instance is
#: never mistaken for the leavings of a crashed one.
TEMPORARY_GRACE_SECONDS: Final = 3600.0

#: Re-stamping an entry on every hit would be a write syscall per tile per grid
#: rebuild. Hour granularity is far finer than the eviction decisions it feeds.
TOUCH_INTERVAL_SECONDS: Final = 3600.0

#: Bumped when anything about the encoded output changes. It is in the key, so a
#: bump invalidates the whole cache instead of leaving entries that decode but
#: no longer look like what this version produces.
CACHE_FORMAT: Final = 1

_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"

#: The terminating IEND chunk, CRC and all. A PNG that does not end with these
#: exact bytes was truncated.
_PNG_TAIL: Final = b"IEND\xaeB\x60\x82"

#: Short enough to be nonsense, and long enough that reading the magic number and
#: reading the tail cannot overlap on the same bytes.
_MINIMUM_PNG_BYTES: Final = len(_PNG_MAGIC) + len(_PNG_TAIL)

#: JPEG, which `to_displayable` passes through untouched, so a cached remote
#: preview is as likely to be one of these as a PNG.
_JPEG_MAGIC: Final = b"\xff\xd8\xff"
_JPEG_TAIL: Final = b"\xff\xd9"
_MINIMUM_JPEG_BYTES: Final = len(_JPEG_MAGIC) + len(_JPEG_TAIL)

#: The name shapes this module writes, and therefore the only ones it will ever
#: delete. Everything else in the directory belongs to someone else.
_ENTRY_NAME: Final = re.compile(r"\A[0-9a-f]{32}\.png\Z")
_TEMPORARY_NAME: Final = re.compile(r"\A\.[0-9a-f]{32}\.[0-9]+\.tmp\.png\Z")

#: Remote previews, cached beside the generated thumbnails so that one
#: directory, one ceiling and one eviction pass cover both. No image extension:
#: a preview is whatever the provider served, and calling a JPEG `.png` would
#: be a lie that a decoder could trip over.
_PREVIEW_NAME: Final = re.compile(r"\A[0-9a-f]{32}\.preview\Z")
_PREVIEW_TEMPORARY_NAME: Final = re.compile(r"\A\.[0-9a-f]{32}\.[0-9]+\.tmp\.preview\Z")


class ThumbnailError(Exception):
    """A thumbnail could not be generated."""


@dataclass(frozen=True, slots=True)
class CacheUsage:
    """What the thumbnail directory currently costs the user."""

    entries: int
    total_bytes: int


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


def cache_directory() -> Path:
    return paths.app_cache_dir() / "thumbnails"


def cache_key(item: MediaItem) -> str:
    """Identity of the *rendered* thumbnail, not just the source file.

    Size and mtime are in the key so replacing a wallpaper in place
    regenerates it, and the geometry is in the key so changing the tile size
    invalidates every thumbnail rather than showing stale ones at the wrong
    dimensions.

    Not a hash of the file's contents. A wallpaper can be a 400 MB video, and
    reading all of it to decide whether to spend 0.3s decoding a frame of it
    would cost more than the decode. Size and mtime come free with the `stat`
    the library scan already did, and the failure they admit -- an edit that
    preserves both -- is not something wallpapers do to themselves.
    """
    source = _thumbnail_source(item)
    try:
        source_info = source.stat()
        source_size, source_mtime = source_info.st_size, int(source_info.st_mtime)
    except OSError:
        source_size, source_mtime = item.size, item.mtime
    material = "\0".join(
        (
            str(CACHE_FORMAT),
            str(item.path),
            str(source),
            str(source_size),
            str(source_mtime),
            item.kind.value,
            f"{THUMBNAIL_WIDTH}x{THUMBNAIL_HEIGHT}",
        )
    )
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def cached_path(item: MediaItem) -> Path:
    """Where ``item``'s thumbnail lives, whether or not anything is there."""
    return cache_directory() / f"{cache_key(item)}.png"


def _opener(path: str, flags: int) -> int:
    # O_NOFOLLOW: a symlink where a cache entry should be is not ours, and is
    # not something to read through whatever it points at.
    return os.open(path, flags | os.O_NOFOLLOW)


def _is_intact(path: Path) -> bool:
    """Whether a cache entry is a complete PNG worth handing to a decoder.

    A cache directory is the one place a half-written file is *expected*: the
    machine can lose power mid-encode, a filesystem can fill up, and people do
    poke around in `~/.cache`. Checking the magic number and the terminating
    chunk costs two seeks, catches both truncation and the file simply not being
    a PNG, and turns either into an ordinary miss.
    """
    try:
        with open(str(path), "rb", opener=_opener) as handle:
            if handle.read(len(_PNG_MAGIC)) != _PNG_MAGIC:
                return False
            if handle.seek(0, os.SEEK_END) < _MINIMUM_PNG_BYTES:
                return False
            handle.seek(-len(_PNG_TAIL), os.SEEK_END)
            return handle.read(len(_PNG_TAIL)) == _PNG_TAIL
    except OSError:
        # Missing, a symlink, a directory, unreadable: all of them mean there is
        # no thumbnail here, and none of them is worth raising over.
        return False


def _is_intact_image(path: Path) -> bool:
    """`_is_intact`, for an entry that may be a JPEG instead of a PNG.

    Remote previews are cached as served. Wallhaven's are JPEG, and running
    them through the PNG check would call every one of them corrupt and cache
    nothing at all.
    """
    try:
        with open(str(path), "rb", opener=_opener) as handle:
            head = handle.read(len(_PNG_MAGIC))
            if head.startswith(_JPEG_MAGIC):
                magic, tail, minimum = _JPEG_MAGIC, _JPEG_TAIL, _MINIMUM_JPEG_BYTES
            elif head == _PNG_MAGIC:
                magic, tail, minimum = _PNG_MAGIC, _PNG_TAIL, _MINIMUM_PNG_BYTES
            else:
                return False
            del magic
            if handle.seek(0, os.SEEK_END) < minimum:
                return False
            handle.seek(-len(tail), os.SEEK_END)
            return handle.read(len(tail)) == tail
    except OSError:
        return False


def _touch(path: Path) -> None:
    """Re-stamp an entry so eviction counts it as recently used.

    Deliberately not `st_atime`. `relatime` is the default nearly everywhere and
    `noatime` is common, so the kernel's own record of last access is either a
    day stale or frozen outright. Owning the timestamp costs one syscall on the
    occasional hit and is the only version that is true on every mount.
    """
    now = time.time()
    try:
        if now - path.stat().st_mtime < TOUCH_INTERVAL_SECONDS:
            return
        os.utime(path, (now, now))
    except OSError:
        # A read-only cache, or the entry evicted underneath us. A lost LRU
        # update makes an entry look older than it is, which costs at worst one
        # regeneration.
        return


def lookup(item: MediaItem) -> Path | None:
    """A usable cached thumbnail for ``item``, or ``None``.

    This is the read side of the cache: it validates before answering, so a
    corrupt entry reports a miss, and it marks the entry used, so showing a
    wallpaper keeps its thumbnail alive. Cheap enough for the main thread --
    a stat and two short reads -- which matters, because delivering a hit
    synchronously is what stops a rebuilt grid flashing through an empty state.
    """
    path = cached_path(item)
    if not _is_intact(path):
        return None
    _touch(path)
    return path


# -- remote previews ------------------------------------------------------
#
# The browse dialog holds its previews in a dict, which is free within one
# session and worth nothing across two: re-opening the browser re-downloads
# every card from the provider's CDN. These four functions are the disk tier
# that fixes that, sharing the thumbnail directory so there is still one place
# to bound and one place to clear.


def preview_key(url: str) -> str:
    """Identity of a cached remote preview.

    The URL alone. A provider's preview URL already names one image on one
    CDN, and unlike a local file there is no mtime to notice a change by --
    which is the right trade here, because a CDN that reuses a URL for
    different bytes has bigger problems than this cache.
    """
    material = "\0".join((str(CACHE_FORMAT), "preview", url))
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def preview_path(url: str) -> Path:
    return cache_directory() / f"{preview_key(url)}.preview"


def lookup_preview(url: str) -> bytes:
    """Cached bytes for ``url``, or empty.

    Bytes rather than a path, because every caller hands them straight to
    `Gdk.Texture.new_from_bytes` and would only have to read the file itself.
    """
    if not url:
        return b""
    path = preview_path(url)
    if not _is_intact_image(path):
        return b""
    try:
        with open(str(path), "rb", opener=_opener) as handle:
            data = handle.read(MAX_PREVIEW_ENTRY_BYTES + 1)
    except OSError:
        return b""
    if len(data) > MAX_PREVIEW_ENTRY_BYTES:
        # Something else wrote this, or an earlier version had a larger
        # ceiling. Either way it is not worth handing to a decoder.
        return b""
    _touch(path)
    return data


def store_preview(url: str, data: bytes) -> None:
    """Cache ``data`` for ``url``. Never raises.

    Written to a temporary and renamed, like `generate`, so a crash or a full
    disk leaves a sweepable temporary rather than a truncated entry that the
    next run would show as a broken picture.

    Failure is silence: a preview that could not be cached is a preview that
    gets downloaded again, which is exactly the situation this improves on
    rather than one it has to guarantee.
    """
    if not url or not data or len(data) > MAX_PREVIEW_ENTRY_BYTES:
        return
    if not data.startswith((*_NATIVE_MAGIC,)):
        # Only what a decoder will take. Caching webp that only ffmpeg can read
        # would mean paying for the transcode on every hit.
        return
    destination = preview_path(url)
    temporary = destination.with_name(f".{preview_key(url)}.{os.getpid()}.tmp.preview")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(str(temporary), "wb", opener=_write_opener) as handle:
            handle.write(data)
        os.replace(temporary, destination)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _write_opener(path: str, flags: int) -> int:
    # O_EXCL as well as O_NOFOLLOW: the temporary name carries this process's
    # pid, so anything already there is a leftover rather than a peer, and
    # opening it would be writing through whatever it has become.
    return os.open(path, flags | os.O_NOFOLLOW | os.O_EXCL, 0o600)


def _command(item: MediaItem, destination: Path) -> list[str]:
    # Scale up to cover the tile, then crop the overflow: a wallpaper grid
    # wants every tile filled, not letterboxed.
    filters = (
        f"scale={THUMBNAIL_WIDTH}:{THUMBNAIL_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMBNAIL_WIDTH}:{THUMBNAIL_HEIGHT}"
    )
    command = ["ffmpeg", "-y", "-v", "error"]
    if item.kind is Kind.VIDEO:
        # Seeking before -i is the fast path: ffmpeg jumps to the keyframe
        # rather than decoding everything up to that point.
        command += ["-ss", str(VIDEO_SEEK_SECONDS)]
    command += [
        "-i",
        str(_thumbnail_source(item)),
        "-vf",
        filters,
        "-frames:v",
        "1",
        str(destination),
    ]
    return command


def _thumbnail_source(item: MediaItem) -> Path:
    """The file ffmpeg can decode for ``item``.

    A Wallpaper Engine scene's path is a directory containing ``scene.pkg``.
    Its generated pairing still is the best representation; the Workshop
    author's preview is the fallback while that still is being made.
    """
    if item.kind is Kind.SCENE:
        if item.paired_still is not None and item.paired_still.is_file():
            return item.paired_still
        if item.preview is not None and item.preview.is_file():
            return item.preview
    return item.path


def generate(item: MediaItem, *, force: bool = False) -> Path:
    """Return a cached thumbnail for ``item``, generating it if needed."""
    if not is_available():
        raise ThumbnailError("ffmpeg is not installed")

    destination = cached_path(item)
    if not force and _is_intact(destination):
        _touch(destination)
        return destination
    source = _thumbnail_source(item)
    if not source.is_file():
        if item.kind is Kind.SCENE:
            raise ThumbnailError(f"no still or preview for scene: {item.name}")
        raise ThumbnailError(f"no such file: {item.path}")

    paths.ensure_directory(destination.parent)
    # Write to a private name and rename: two workers racing on the same item,
    # or a crash mid-encode, must never leave a half-written PNG in the cache.
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.png")
    try:
        completed = subprocess.run(
            _command(item, temporary),
            capture_output=True,
            timeout=GENERATE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        temporary.unlink(missing_ok=True)
        raise ThumbnailError(f"timed out thumbnailing {item.path.name}") from error
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ThumbnailError(f"cannot run ffmpeg: {error}") from error

    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ThumbnailError(
            f"ffmpeg failed on {item.path.name}: {detail[-1] if detail else 'no output'}"
        )

    os.replace(temporary, destination)
    # Bound the directory here rather than leaving it to the caller: every path
    # that grows the cache runs through this line, and a scan costs a couple of
    # milliseconds against the 0.3s decode that just happened.
    prune()
    return destination


#: What `Gdk.Texture.new_from_bytes` decodes on its own, by magic number. Every
#: other format a provider might hand us -- webp above all -- goes through
#: ffmpeg first, for the same reason local thumbnails do.
_NATIVE_MAGIC: Final[tuple[bytes, ...]] = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")

#: A remote preview is a few hundred KB; a transcode of one should be quick.
DECODE_TIMEOUT: Final = 10.0


def is_natively_decodable(data: bytes) -> bool:
    return data.startswith(_NATIVE_MAGIC)


def to_displayable(data: bytes) -> bytes:
    """Bytes GTK can turn into a texture, transcoding through ffmpeg if needed.

    Remote thumbnails arrive as whatever the provider serves, and MotionBGS
    serves webp. Returns empty rather than raising: a preview that will not
    decode is a card without a picture, not a failure worth a dialog.
    """
    if not data:
        return b""
    if is_natively_decodable(data):
        return data
    if not is_available():
        return b""
    try:
        completed = subprocess.run(
            # `-` for both ends: nothing about a downloaded preview should
            # reach the filesystem on its way to being looked at.
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-c:v",
                "png",
                "-f",
                "image2",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            timeout=DECODE_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return b""
    if completed.returncode != 0:
        return b""
    return completed.stdout if is_natively_decodable(completed.stdout) else b""


@dataclass(frozen=True, slots=True)
class _Entry:
    """One file in the cache directory that this module wrote."""

    path: Path
    size: int
    used_at: float


def _scan() -> tuple[list[_Entry], list[_Entry]]:
    """Cache entries and abandoned temporaries, each least-recently-used first.

    Only files whose names this module could have written are returned, and only
    if they are regular files. That single rule is what keeps eviction honest:
    a foreign file dropped in the directory is never counted and never deleted,
    and a symlink is never followed to whatever it points at outside.
    """
    entries: list[_Entry] = []
    temporaries: list[_Entry] = []
    try:
        with os.scandir(cache_directory()) as found:
            for candidate in found:
                if _ENTRY_NAME.match(candidate.name) or _PREVIEW_NAME.match(candidate.name):
                    bucket = entries
                elif _TEMPORARY_NAME.match(candidate.name) or _PREVIEW_TEMPORARY_NAME.match(
                    candidate.name
                ):
                    bucket = temporaries
                else:
                    continue
                try:
                    status = candidate.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(status.st_mode):
                    continue
                bucket.append(_Entry(Path(candidate.path), status.st_size, status.st_mtime))
    except OSError:
        # No cache directory yet, or one we cannot read. Either way there is
        # nothing here to account for.
        return [], []
    entries.sort(key=lambda entry: entry.used_at)
    temporaries.sort(key=lambda entry: entry.used_at)
    return entries, temporaries


def _discard(entry: _Entry) -> bool:
    """Unlink one entry, tolerating a second instance having got there first."""
    try:
        entry.path.unlink()
    except FileNotFoundError:
        # Two instances evicting the same entry. Gone is gone.
        return True
    except OSError:
        return False
    return True


def usage() -> CacheUsage:
    """How many thumbnails are cached and what they weigh."""
    entries, temporaries = _scan()
    both = (*entries, *temporaries)
    return CacheUsage(entries=len(entries), total_bytes=sum(entry.size for entry in both))


def prune(max_bytes: int = MAX_CACHE_BYTES) -> int:
    """Evict least-recently-used thumbnails until the cache fits ``max_bytes``.

    Returns the number of files removed. Safe to run while another instance is
    reading the same directory: eviction only ever unlinks, readers hold their
    own descriptors, and a reader that loses the race sees a miss and
    regenerates. Two instances pruning at once simply agree.

    Entries used within :data:`EVICTION_GRACE_SECONDS` are left alone even if
    that means finishing over the ceiling, which is the right trade -- being
    briefly over is cheaper than throwing away a thumbnail somebody is about to
    draw. The next prune collects them.
    """
    moment = time.time()
    entries, temporaries = _scan()

    removed = 0
    for temporary in temporaries:
        # A temporary this old is the leavings of a crashed or killed encode;
        # nothing live writes to one for an hour.
        if moment - temporary.used_at > TEMPORARY_GRACE_SECONDS and _discard(temporary):
            removed += 1

    total = sum(entry.size for entry in entries)
    if total <= max_bytes:
        return removed

    target = int(max(max_bytes, 0) * PRUNE_TARGET_RATIO)
    for entry in entries:
        if total <= target:
            break
        if moment - entry.used_at < EVICTION_GRACE_SECONDS:
            continue
        if _discard(entry):
            removed += 1
            total -= entry.size
    return removed


def clear() -> int:
    """Delete every thumbnail this app wrote, and report how many.

    Here for a Settings button to call. The old plugin left 108 files and 2.4 MB
    in its state directory with nothing anywhere that could remove them; the
    fact that they were small was luck, not design.
    """
    entries, temporaries = _scan()
    return sum(_discard(entry) for entry in (*entries, *temporaries))
