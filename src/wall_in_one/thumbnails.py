"""Thumbnail cache.

Everything goes through ffmpeg, stills included. That looks like overkill for a
PNG until you notice this closure's GdkPixbuf has no webp or avif loader, while
`IMAGE_EXTENSIONS` has both -- and that videos need a frame grab regardless. One
tool that handles every format we accept beats branching on which pixbuf loaders
happen to be installed.

Measured at ~0.3s per thumbnail, including seeking a 4K mp4, so generation
belongs off the main thread. See `wall_in_one.ui.thumbnails` for that part.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
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

#: Cache ceiling. At roughly 100 KB a thumbnail this is a few hundred MB worst
#: case, and the library scan tops out at 4096 items anyway.
MAX_CACHE_ENTRIES: Final = 4096


class ThumbnailError(Exception):
    """A thumbnail could not be generated."""


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
    """
    material = "\0".join(
        (
            str(item.path),
            str(item.size),
            str(item.mtime),
            item.kind.value,
            f"{THUMBNAIL_WIDTH}x{THUMBNAIL_HEIGHT}",
        )
    )
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def cached_path(item: MediaItem) -> Path:
    return cache_directory() / f"{cache_key(item)}.png"


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
    command += ["-i", str(item.path), "-vf", filters, "-frames:v", "1", str(destination)]
    return command


def generate(item: MediaItem, *, force: bool = False) -> Path:
    """Return a cached thumbnail for ``item``, generating it if needed."""
    if not is_available():
        raise ThumbnailError("ffmpeg is not installed")

    destination = cached_path(item)
    if not force and destination.is_file():
        return destination
    if not item.path.is_file():
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


def prune(limit: int = MAX_CACHE_ENTRIES) -> int:
    """Drop the least recently modified thumbnails past ``limit``."""
    directory = cache_directory()
    try:
        entries = [entry for entry in directory.iterdir() if entry.suffix == ".png"]
    except OSError:
        return 0
    if len(entries) <= limit:
        return 0

    def age(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=age)
    removed = 0
    for entry in entries[: len(entries) - limit]:
        try:
            entry.unlink()
        except OSError:
            continue
        removed += 1
    return removed
