"""Making the still that stands behind a video wallpaper.

`pairing` is the read half: it finds the still a video already has, by sidecar,
by the managed `Automatic Stills` directory, or by the naming convention the
user's own library follows. This is the write half, split off for the same
reason `credentials` is split from `registry` -- finding a still has to work
everywhere, while making one shells out to ffmpeg and writes to disk, and the
two want very different tests.

Until this existed, two of the three ways a video could get a still were
unreachable: nothing wrote a sidecar, and nothing ever put a file in
`Automatic Stills`. Only the user's own `foo-still.png` convention worked, so
a downloaded video had nothing to show when dynamics were switched off. The
applier's answer was to refuse -- "is a video with no still, and dynamics are
off" -- and Noctalia's palette went on being derived from whatever still was
set last, which is the wrong colours for the wallpaper actually on screen.

The frame is taken a few seconds in. Videos routinely open on black or on a
fade, and a black still is worse than no still: it looks like a bug, and the
palette generated from it is grey.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.library import pairing
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.wallpaper import scenes

#: Where to seek before grabbing the frame. Far enough in to clear an opening
#: fade, near enough that a short loop still has something there.
SEEK_SECONDS: Final = 3.0

#: Retried at the very start when the seek lands past the end of a short clip.
FALLBACK_SEEK_SECONDS: Final = 0.0

#: A single frame out of a keyframe seek is quick. This is the ceiling for a
#: pathological file, not a budget.
GENERATE_TIMEOUT: Final = 60.0

#: PNG, deliberately. A still is a frame grab that Noctalia will then derive a
#: palette from, and re-quantising it through JPEG shifts the colours it reads.
STILL_SUFFIX: Final = ".png"

#: Difference tolerated when comparing an existing still with the target
#: display. Small rounding differences from compositor scaling are harmless;
#: the old portrait capture is nowhere near this bound.
ASPECT_TOLERANCE: Final = 0.04


class StillError(Exception):
    """A still could not be made. Never fatal: the video still plays."""


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG's IHDR dimensions without pulling image decoding into scans."""
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return (width, height) if width > 0 and height > 0 else None


def scene_capture_required(
    item: MediaItem,
    root: Path,
    *,
    size: tuple[int, int] | None = None,
) -> bool:
    """Whether an automatic scene still is absent, undersized, or wrong-shaped.

    Only the managed automatic filename is eligible. A custom still selected
    from the library may intentionally have another shape and must never be
    overwritten by this maintenance path.
    """
    if item.kind is not Kind.SCENE or not item.scene:
        return False
    target = pairing.still_directory(root) / f"{item.scene}{STILL_SUFFIX}"
    if item.paired_still is not None and item.paired_still != target:
        return False
    actual = _png_size(target)
    if actual is None:
        return True
    wanted = size or scenes.capture_size()
    width, height = actual
    wanted_width, wanted_height = wanted
    aspect_error = abs(width / height - wanted_width / wanted_height)
    return aspect_error > ASPECT_TOLERANCE or width < wanted_width or height < wanted_height


def destination(video: Path, root: Path) -> Path:
    """Where the generated still for ``video`` belongs under ``root``.

    The name matches the video's, because that is what `pairing` looks for
    when it searches the managed directory.
    """
    return pairing.still_directory(root) / f"{video.stem}{STILL_SUFFIX}"


def write_sidecar(video: Path, still: Path) -> Path:
    """Record that ``still`` represents ``video``, and return the sidecar.

    Written even when the still sits in `Automatic Stills`, where it would be
    found anyway. The sidecar is the only one of the three pairing rules that
    survives the still being moved, and it is what makes a hand-picked still
    stick when the conventions would choose a different one.
    """
    path = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    payload = json.dumps({pairing.SIDECAR_STILL_KEY: str(still)}, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(f"could not write {path}: {error.strerror or error}") from error
    return path


def is_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _command(video: Path, target: Path, seek: float) -> list[str]:
    # Seeking before -i is the fast path: ffmpeg jumps to the nearest keyframe
    # instead of decoding everything up to that point. Full resolution and no
    # filters -- this is a wallpaper, not a thumbnail.
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        str(seek),
        "-i",
        str(video),
        "-frames:v",
        "1",
        str(target),
    ]


def _run(video: Path, target: Path, seek: float) -> str:
    """Grab one frame, returning ffmpeg's complaint or ``""`` on success."""
    try:
        completed = subprocess.run(
            _command(video, target, seek),
            capture_output=True,
            timeout=GENERATE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise StillError(f"timed out taking a still from {video.name}") from error
    except OSError as error:
        raise StillError(f"cannot run ffmpeg: {error}") from error
    if completed.returncode == 0 and target.is_file() and target.stat().st_size > 0:
        return ""
    detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
    return detail[-1] if detail else "no output"


def generate(video: Path, root: Path, *, force: bool = False) -> Path:
    """Take a still from ``video`` into ``root``, and pair the two.

    Returns the still. An existing one is reused rather than re-encoded unless
    ``force``, so this is cheap to call on a video that already has one.
    """
    if not is_available():
        raise StillError("ffmpeg is not installed, so no still can be taken")
    if not video.is_file():
        raise StillError(f"no such file: {video}")

    target = destination(video, root)
    if not force and target.is_file() and target.stat().st_size > 0:
        _record_beside(video, target, root)
        return target

    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise StillError(f"could not create {target.parent}: {error.strerror or error}") from error

    # A half-written still is worse than none: `pairing` would find it, and the
    # user would get a torn frame as their wallpaper.
    temporary = target.with_name(f".{target.stem}.{os.getpid()}.tmp{STILL_SUFFIX}")
    try:
        complaint = _run(video, temporary, SEEK_SECONDS)
        if complaint:
            # The seek landing past the end of a short loop is the ordinary way
            # this fails, and the first frame is a fine answer for a clip that
            # short. Anything still wrong after that is worth reporting.
            complaint = _run(video, temporary, FALLBACK_SEEK_SECONDS)
        if complaint:
            raise StillError(f"ffmpeg could not take a still from {video.name}: {complaint}")
        os.replace(temporary, target)
    except StillError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(f"could not write {target}: {error.strerror or error}") from error

    _record_beside(video, target, root)
    return target


def _record_beside(video: Path, still: Path, root: Path) -> None:
    """Write the sidecar, but only into a directory this app is entitled to.

    A Wallpaper Engine wallpaper lives in Steam's Workshop tree, and writing
    into it is not ours to do -- Steam may replace the directory wholesale, and
    a foreign file in there is litter in somebody else's collection. The still
    itself lands under the managed `Automatic Stills` directory either way, and
    `pairing` finds it there by name, so the sidecar is belt to that braces
    rather than the only record.
    """
    try:
        inside = video.is_relative_to(root)
    except (OSError, ValueError):
        inside = False
    if not inside:
        return
    write_sidecar(video, still)


def capture_scene(item: MediaItem, root: Path, *, force: bool = False) -> Path:
    """Take a still from a Wallpaper Engine scene, through the engine itself.

    ffmpeg cannot help here: a scene has no file to decode, only a `scene.pkg`
    that `linux-wallpaperengine` knows how to read. The engine renders it in a
    window and writes one frame, which is why this can run while somebody
    else's engine owns the screen -- see `wallpaper.scenes`.

    The still is named by the Workshop id rather than by the directory, so a
    reinstall that moves the directory still finds it.
    """
    if not item.scene:
        raise StillError(f"{item.name} is not a Wallpaper Engine scene")
    target = pairing.still_directory(root) / f"{item.scene}{STILL_SUFFIX}"
    size = scenes.capture_size()
    if not force and not scene_capture_required(item, root, size=size):
        return target
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise StillError(f"could not create {target.parent}: {error.strerror or error}") from error
    temporary = target.with_name(f".{target.stem}.{os.getpid()}.tmp{target.suffix}")
    try:
        scenes.screenshot(item.scene, temporary, size=size)
        os.replace(temporary, target)
        return target
    except scenes.SceneError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(str(error)) from error
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(f"could not replace {target}: {error.strerror or error}") from error


def ensure(item: MediaItem, root: Path) -> Path | None:
    """The still for ``item``, making one if it has none. ``None`` if it needs none.

    The forgiving entry point, for callers that want a still if one can be had
    and can carry on without: a still that cannot be made is not a reason to
    refuse to play the wallpaper.
    """
    if not item.is_moving:
        return None
    if item.kind is Kind.SCENE:
        if not scene_capture_required(item, root):
            return item.paired_still or (
                pairing.still_directory(root) / f"{item.scene}{STILL_SUFFIX}"
            )
        try:
            return capture_scene(item, root)
        except StillError:
            return None
    if item.paired_still is not None:
        return item.paired_still
    existing = pairing.find_still(item.path, roots=(root,))
    if existing is not None:
        return existing
    try:
        return generate(item.path, root)
    except StillError:
        return None
