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

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from wall_in_one import file_io, paths, worker_processes
from wall_in_one.library import pairing, state_file
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

# Publication and deletion meet at this lock rather than around the expensive
# renderer call.  The files are deliberately never unlinked: replacing a
# flock file while another process has its inode open would allow a third
# process to enter beside it.  XDG_RUNTIME_DIR is session-cleaned in the usual
# case; the cache fallback retains only tiny, fixed-hash files.
LIFECYCLE_LOCK_DIRECTORY: Final = f"{paths.APP_ID}-still-lifecycles"
LIFECYCLE_LOCK_TIMEOUT_SECONDS: Final = 5.0
LIFECYCLE_LOCK_POLL_SECONDS: Final = 0.025


class StillError(Exception):
    """A still could not be made. Never fatal: the video still plays."""


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    """The exact filesystem object rendered into one automatic still."""

    device: int
    inode: int
    file_type: int
    changed_ns: int
    size: int | None
    modified_ns: int | None
    descriptor: int = field(compare=False, repr=False)

    def close(self) -> None:
        os.close(self.descriptor)


def _lifecycle_material(path: Path, kind: Kind, scene: str = "") -> bytes:
    """Stable media identity shared by publication and artifact cleanup."""
    if kind is Kind.SCENE and scene:
        source = scene.encode("utf-8", "surrogatepass")
    else:
        source = os.fsencode(path.absolute())
    return b"\0".join((kind.value.encode("ascii"), source))


def _lifecycle_lock_path(path: Path, kind: Kind, scene: str = "") -> Path:
    identity = hashlib.sha256(_lifecycle_material(path, kind, scene)).hexdigest()
    directory = paths.runtime_dir() / LIFECYCLE_LOCK_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    opened = directory.lstat()
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(opened.st_mode)
        or opened.st_uid != os.getuid()
    ):
        raise OSError(f"automatic-still lifecycle path is not a private directory: {directory}")
    os.chmod(directory, 0o700, follow_symlinks=False)
    return directory / f"{identity}.lock"


@contextlib.contextmanager
def _source_lifecycle_lock(
    path: Path,
    kind: Kind,
    scene: str = "",
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Serialize the short publication/cleanup commit for one media identity."""
    wait = LIFECYCLE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if wait < 0:
        raise OSError("automatic-still lifecycle lock timeout cannot be negative")
    deadline = time.monotonic() + wait
    lock_path = _lifecycle_lock_path(path, kind, scene)
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise OSError(
                f"automatic-still lifecycle lock {lock_path} is not a private regular file"
            )
        os.fchmod(descriptor, 0o600)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {wait:g}s waiting for automatic-still lifecycle "
                        f"lock {lock_path}"
                    ) from None
                time.sleep(min(LIFECYCLE_LOCK_POLL_SECONDS, remaining))
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"automatic-still lifecycle lock {lock_path} changed while waiting")
        yield
    finally:
        if descriptor is not None:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


@contextlib.contextmanager
def source_lifecycle_lock(item: MediaItem) -> Iterator[None]:
    """Share an item's publication boundary with delete/trash/uninstall cleanup."""
    with _source_lifecycle_lock(item.path, item.kind, item.scene):
        yield


def _snapshot_source(path: Path, kind: Kind) -> _SourceSnapshot:
    """Remember the exact regular video or scene directory about to render."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    if kind is Kind.SCENE:
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StillError(f"no such source: {path}") from error
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        valid_type = (
            stat.S_ISDIR(opened.st_mode) if kind is Kind.SCENE else stat.S_ISREG(opened.st_mode)
        )
        if (
            not valid_type
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(current.st_mode)
        ):
            expected = "directory" if kind is Kind.SCENE else "regular video file"
            raise StillError(f"{path} is not the same {expected} that was opened")
        return _SourceSnapshot(
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_ctime_ns,
            opened.st_size if kind is Kind.VIDEO else None,
            opened.st_mtime_ns if kind is Kind.VIDEO else None,
            descriptor,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _require_unchanged_source(path: Path, kind: Kind, expected: _SourceSnapshot) -> None:
    """Fail closed if delete/reinstall won while capture ran outside the lock."""
    try:
        current = path.lstat()
    except OSError as error:
        raise StillError(f"{path} was removed while its still was being made") from error
    actual = _SourceSnapshot(
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
        current.st_ctime_ns,
        current.st_size if kind is Kind.VIDEO else None,
        current.st_mtime_ns if kind is Kind.VIDEO else None,
        expected.descriptor,
    )
    valid_type = (
        stat.S_ISDIR(current.st_mode) if kind is Kind.SCENE else stat.S_ISREG(current.st_mode)
    )
    if not valid_type or actual != expected:
        raise StillError(f"{path} changed while its still was being made")


def _nonempty_regular(path: Path) -> bool:
    """Whether a publication candidate is a real, nonempty regular file."""
    try:
        current = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and current.st_size > 0


def _png_size(path: Path) -> tuple[int, int] | None:
    """Read a PNG's IHDR dimensions without pulling image decoding into scans."""
    try:
        header = file_io.read_regular_prefix(path, 24)
    except OSError:
        return None
    if header is None:
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

    The name is keyed by the video's absolute path, because basenames are not
    identities: two library folders may both contain an ``intro.mp4``.
    """
    return pairing.still_directory(root) / f"{pairing.automatic_still_stem(video)}{STILL_SUFFIX}"


def automatic_destination(item: MediaItem, root: Path) -> Path | None:
    """The exact app-generated still owned by ``item`` under ``root``.

    Keeping this derivation shared with library de-duplication and deletion is
    important: a manually selected image may represent a moving wallpaper,
    but only this deterministic path is an app-owned child of that wallpaper.
    """
    if item.kind is Kind.VIDEO:
        return destination(item.path, root)
    if item.kind is Kind.SCENE and item.scene:
        return pairing.still_directory(root) / f"{item.scene}{STILL_SUFFIX}"
    return None


def write_sidecar(video: Path, still: Path) -> Path:
    """Record that ``still`` represents ``video``, and return the sidecar.

    Written even when the still sits in `Automatic Stills`, where it would be
    found anyway. The sidecar is the only one of the three pairing rules that
    survives the still being moved, and it is what makes a hand-picked still
    stick when the conventions would choose a different one.
    """
    path = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    payload = json.dumps({pairing.SIDECAR_STILL_KEY: str(still)}, indent=2) + "\n"
    try:
        state_file.write_atomic_text(path, payload)
    except OSError as error:
        raise StillError(f"could not write {path}: {error.strerror or error}") from error
    return path


def _private_image_temporary(target: Path) -> Path:
    """Reserve an unpredictable same-directory name for an external writer."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.stem}.",
        suffix=f".tmp{target.suffix}",
        dir=target.parent,
    )
    os.close(descriptor)
    return Path(name)


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


def _run(
    video: Path,
    target: Path,
    seek: float,
    *,
    processes: worker_processes.Cancellation | None = None,
) -> str:
    """Grab one frame, returning ffmpeg's complaint or ``""`` on success."""
    try:
        completed = (
            processes.run(_command(video, target, seek), timeout=GENERATE_TIMEOUT)
            if processes is not None
            else subprocess.run(
                _command(video, target, seek),
                capture_output=True,
                timeout=GENERATE_TIMEOUT,
                check=False,
            )
        )
    except worker_processes.ProcessCancelledError as error:
        raise StillError(f"cancelled taking a still from {video.name}") from error
    except subprocess.TimeoutExpired as error:
        raise StillError(f"timed out taking a still from {video.name}") from error
    except OSError as error:
        raise StillError(f"cannot run ffmpeg: {error}") from error
    if completed.returncode == 0 and target.is_file() and target.stat().st_size > 0:
        return ""
    detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
    return detail[-1] if detail else "no output"


def generate(
    video: Path,
    root: Path,
    *,
    force: bool = False,
    processes: worker_processes.Cancellation | None = None,
) -> Path:
    """Take a still from ``video`` into ``root``, and pair the two.

    Returns the still. An existing one is reused rather than re-encoded unless
    ``force``, so this is cheap to call on a video that already has one.
    """
    if not is_available():
        raise StillError("ffmpeg is not installed, so no still can be taken")
    source = _snapshot_source(video, Kind.VIDEO)
    try:
        return _generate_from_source(
            video,
            root,
            force=force,
            processes=processes,
            source=source,
        )
    finally:
        source.close()


def _generate_from_source(
    video: Path,
    root: Path,
    *,
    force: bool,
    processes: worker_processes.Cancellation | None,
    source: _SourceSnapshot,
) -> Path:
    """Capture and publish while retaining the opened source identity."""

    target = destination(video, root)
    if not force:
        try:
            # Reusing a target can still publish its sidecar. It therefore has
            # the same lifecycle boundary as a newly rendered frame.
            with _source_lifecycle_lock(video, Kind.VIDEO):
                _require_unchanged_source(video, Kind.VIDEO, source)
                if _nonempty_regular(target):
                    _record_beside(video, target, root)
                    return target
        except StillError:
            raise
        except OSError as error:
            raise StillError(
                f"could not safely publish a still for {video.name}: {error}"
            ) from error

    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise StillError(f"could not create {target.parent}: {error.strerror or error}") from error

    # A half-written still is worse than none: `pairing` would find it, and the
    # user would get a torn frame as their wallpaper.
    try:
        temporary = _private_image_temporary(target)
    except OSError as error:
        raise StillError(
            f"could not create a temporary still in {target.parent}: {error}"
        ) from error
    try:
        complaint = _run(video, temporary, SEEK_SECONDS, processes=processes)
        if complaint:
            # The seek landing past the end of a short loop is the ordinary way
            # this fails, and the first frame is a fine answer for a clip that
            # short. Anything still wrong after that is worth reporting.
            complaint = _run(video, temporary, FALLBACK_SEEK_SECONDS, processes=processes)
        if complaint:
            raise StillError(f"ffmpeg could not take a still from {video.name}: {complaint}")
        # ffmpeg can take minutes on a pathological input; never make Delete,
        # Trash or Workshop cleanup wait for it. Only the short commit is
        # serialized, and the exact source captured above is checked before
        # either the image or its sidecar becomes visible.
        with _source_lifecycle_lock(video, Kind.VIDEO):
            _require_unchanged_source(video, Kind.VIDEO, source)
            os.replace(temporary, target)
            state_file.fsync_parent(target)
            _record_beside(video, target, root)
    except StillError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(f"could not write {target}: {error.strerror or error}") from error

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
    except OSError, ValueError:
        inside = False
    if not inside:
        return
    write_sidecar(video, still)


def capture_scene(
    item: MediaItem,
    root: Path,
    *,
    force: bool = False,
    processes: worker_processes.Cancellation | None = None,
) -> Path:
    """Take a still from a Wallpaper Engine scene, through the engine itself.

    ffmpeg cannot help here: a scene has no file to decode, only a `scene.pkg`
    that `linux-wallpaperengine` knows how to read. The engine renders it in a
    window and writes one frame, which is why this can run while somebody
    else's engine owns the screen -- see `wallpaper.scenes`.

    The still is named by the Workshop id rather than by the directory, so a
    reinstall that moves the directory still finds it.
    """
    if item.kind is not Kind.SCENE or not item.scene:
        raise StillError(f"{item.name} is not a Wallpaper Engine scene")
    target = pairing.still_directory(root) / f"{item.scene}{STILL_SUFFIX}"
    size = scenes.capture_size()
    if not force and not scene_capture_required(item, root, size=size):
        return target
    source = _snapshot_source(item.path, Kind.SCENE)
    try:
        return _capture_scene_from_source(
            item,
            target=target,
            size=size,
            processes=processes,
            source=source,
        )
    finally:
        source.close()


def _capture_scene_from_source(
    item: MediaItem,
    *,
    target: Path,
    size: tuple[int, int],
    processes: worker_processes.Cancellation | None,
    source: _SourceSnapshot,
) -> Path:
    """Render and publish a scene while retaining its opened directory identity."""
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise StillError(f"could not create {target.parent}: {error.strerror or error}") from error
    try:
        temporary = _private_image_temporary(target)
    except OSError as error:
        raise StillError(
            f"could not create a temporary still in {target.parent}: {error}"
        ) from error
    try:
        if processes is None:
            scenes.screenshot(item.scene, temporary, size=size)
        else:
            scenes.screenshot(item.scene, temporary, size=size, processes=processes)
        with _source_lifecycle_lock(item.path, Kind.SCENE, item.scene):
            _require_unchanged_source(item.path, Kind.SCENE, source)
            os.replace(temporary, target)
            state_file.fsync_parent(target)
        return target
    except StillError:
        temporary.unlink(missing_ok=True)
        raise
    except scenes.SceneError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(str(error)) from error
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise StillError(f"could not replace {target}: {error.strerror or error}") from error


def ensure(
    item: MediaItem,
    root: Path,
    *,
    processes: worker_processes.Cancellation | None = None,
) -> Path | None:
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
            return capture_scene(item, root, processes=processes)
        except StillError:
            return None
    if item.paired_still is not None:
        return item.paired_still
    existing = pairing.find_still(item.path, roots=(root,))
    if existing is not None:
        return existing
    try:
        return generate(item.path, root, processes=processes)
    except StillError:
        return None
