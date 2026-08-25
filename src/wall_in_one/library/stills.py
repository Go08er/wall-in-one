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
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from wall_in_one import file_io, paths, worker_processes
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


def _close_descriptor_preserving_error(descriptor: int, purpose: str) -> None:
    """Close one capability without replacing an exception already in flight."""
    active_error = sys.exception()
    try:
        os.close(descriptor)
    except OSError as close_error:
        if active_error is not None:
            active_error.add_note(f"also could not close {purpose}: {close_error}")


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    """The exact filesystem object rendered into one automatic still."""

    device: int
    inode: int
    file_type: int
    changed_ns: int
    size: int | None
    modified_ns: int | None
    parent_device: int
    parent_inode: int
    parent_changed_ns: int | None
    parent_modified_ns: int | None
    descriptor: int = field(compare=False, repr=False)
    parent_pin: file_io.PinnedPath = field(compare=False, repr=False)

    @property
    def writer_path(self) -> Path:
        """Descriptor path an external renderer can open from this process."""
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.descriptor)

    def close(self) -> None:
        descriptor = self.descriptor
        if descriptor < 0:
            return
        object.__setattr__(self, "descriptor", -1)
        try:
            _close_descriptor_preserving_error(descriptor, "the automatic-still source pin")
        finally:
            self.parent_pin.close()


@dataclass(slots=True)
class _ImageTemporary:
    """One atomically created image inode retained across an external write."""

    path: Path
    logical_path: Path
    pin: file_io.PinnedPath | None = field(repr=False)
    published: bool = False

    @property
    def writer_path(self) -> Path:
        """Exact pre-created output inode usable from the external child."""
        if self.pin is None:
            raise OSError(f"the private still output {self.logical_path} is closed")
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.pin.descriptor)

    def retain_rendered(self) -> file_io.FileFingerprint:
        """Pin and validate the exact nonempty output produced by the renderer."""
        if self.pin is None:
            raise file_io.PathChangedError(
                f"the private still output at {self.logical_path} is closed"
            )
        fingerprint = self.pin.fingerprint
        if fingerprint[2] <= 0:
            raise file_io.PathChangedError(f"the private still output at {self.path} is empty")
        return fingerprint

    def mark_published(self) -> None:
        self.published = True

    def close(self) -> None:
        """Release only the exact output inode created by ``mkstemp``."""
        pin = self.pin
        try:
            if pin is not None and not self.published:
                with contextlib.suppress(OSError, ValueError):
                    file_io.discard_regular_if_same(
                        self.path,
                        expected_identity=pin.identity,
                        expected_fingerprint=pin.fingerprint,
                        pinned_source=pin,
                        retained_parent=self.path.parent,
                        logical_retained_parent=self.logical_path.parent,
                    )
        finally:
            if pin is not None:
                pin.close()
            self.pin = None


@dataclass(slots=True)
class _ExistingTarget:
    """The exact app-reserved target generation frozen before rendering."""

    pin: file_io.PinnedPath = field(repr=False)
    fingerprint: file_io.FileFingerprint

    def close(self) -> None:
        self.pin.close()


@dataclass(slots=True)
class _ImagePublication:
    """A public still generation retaining exact commit and rollback authority."""

    target: Path
    access: Path
    context: file_io.PinnedDirectoryContext = field(repr=False)
    fingerprint: file_io.FileFingerprint
    pin: file_io.PinnedPath = field(repr=False)
    prior: file_io.ClaimedPath | None = field(default=None, repr=False)
    settled: bool = False
    _closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        _require_public_target(
            self.context,
            self.target,
            self.access,
            self.fingerprint,
        )

    def commit(self) -> None:
        if self.settled:
            return
        self.verify()
        if self.prior is not None:
            self.prior.discard()
        self.verify()
        self.settled = True

    def rollback(self) -> None:
        if self.settled:
            return
        published_claim: file_io.ClaimedPath | None = None
        try:
            published_claim = file_io.claim_for_deletion(
                self.access,
                expected_identity=self.fingerprint[:2],
                expected_fingerprint=self.fingerprint,
                pinned_source=self.pin,
                logical_path=self.target,
            )
            if self.prior is not None and not self.prior.restore():
                raise file_io.PathChangedError(
                    f"could not restore the prior automatic still at {self.target}",
                    preserved_path=self.prior.path,
                )
            published_claim.discard()
            paths.fsync_directory(self.access.parent)
            self.settled = True
        finally:
            if published_claim is not None:
                published_claim.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.prior is not None:
                self.prior.close()
                self.prior = None
        finally:
            self.pin.close()


def _lifecycle_material(path: Path, kind: Kind, scene: str = "") -> bytes:
    """Stable media identity shared by publication and artifact cleanup."""
    if kind is Kind.SCENE and scene:
        source = scene.encode("utf-8", "surrogatepass")
    else:
        source = os.fsencode(path.absolute())
    return b"\0".join((kind.value.encode("ascii"), source))


def _lifecycle_lock_path_from_material(material: bytes) -> Path:
    """Private flock path for one stable, namespaced lifecycle identity."""
    identity = hashlib.sha256(material).hexdigest()
    directory = paths.runtime_dir() / LIFECYCLE_LOCK_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    opened = directory.lstat()
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(opened.st_mode)
        or opened.st_uid != os.getuid()
    ):
        raise OSError(f"media lifecycle path is not a private directory: {directory}")
    os.chmod(directory, 0o700, follow_symlinks=False)
    return directory / f"{identity}.lock"


def _lifecycle_lock_path(path: Path, kind: Kind, scene: str = "") -> Path:
    return _lifecycle_lock_path_from_material(_lifecycle_material(path, kind, scene))


@contextlib.contextmanager
def _lifecycle_lock(
    lock_path: Path,
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Hold one trusted lifecycle flock for a bounded commit section."""
    wait = LIFECYCLE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if wait < 0:
        raise OSError("media lifecycle lock timeout cannot be negative")
    deadline = time.monotonic() + wait
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
            raise OSError(f"media lifecycle lock {lock_path} is not a private regular file")
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
                        f"timed out after {wait:g}s waiting for media lifecycle lock {lock_path}"
                    ) from None
                time.sleep(min(LIFECYCLE_LOCK_POLL_SECONDS, remaining))
        opened = os.fstat(descriptor)
        current = lock_path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"media lifecycle lock {lock_path} changed while waiting")
        yield
    finally:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(owned_descriptor, fcntl.LOCK_UN)
            _close_descriptor_preserving_error(
                owned_descriptor,
                f"media lifecycle lock {lock_path}",
            )


@contextlib.contextmanager
def _source_lifecycle_lock(
    path: Path,
    kind: Kind,
    scene: str = "",
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Serialize the short still publication/cleanup commit for one source."""
    with _lifecycle_lock(_lifecycle_lock_path(path, kind, scene), timeout=timeout):
        yield


@contextlib.contextmanager
def media_path_lifecycle_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Serialize provider publication and removal at one logical media path."""
    material = b"media-path\0" + os.fsencode(path.absolute())
    with _lifecycle_lock(_lifecycle_lock_path_from_material(material), timeout=timeout):
        yield


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
        parent_status = path.parent.lstat()
    except OSError as error:
        raise StillError(f"no such source: {path}") from error
    if not stat.S_ISDIR(parent_status.st_mode):
        raise StillError(f"no such source directory: {path.parent}")
    try:
        parent_pin = file_io.pin_directory_path(
            path.parent,
            expected_identity=(parent_status.st_dev, parent_status.st_ino),
        )
    except OSError as error:
        raise StillError(f"no such source: {path}") from error
    access = Path("/proc/self/fd") / str(parent_pin.descriptor) / path.name
    try:
        descriptor = os.open(access, flags)
    except OSError as error:
        parent_pin.close()
        raise StillError(f"no such source: {path}") from error
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        retained_parent = parent_pin.status()
        current_parent = path.parent.lstat()
        valid_type = (
            stat.S_ISDIR(opened.st_mode) if kind is Kind.SCENE else stat.S_ISREG(opened.st_mode)
        )
        if (
            not valid_type
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(current.st_mode)
            or (retained_parent.st_dev, retained_parent.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
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
            retained_parent.st_dev,
            retained_parent.st_ino,
            retained_parent.st_ctime_ns if kind is Kind.SCENE else None,
            retained_parent.st_mtime_ns if kind is Kind.SCENE else None,
            descriptor,
            parent_pin,
        )
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            parent_pin.close()
        raise


def _require_unchanged_source(path: Path, kind: Kind, expected: _SourceSnapshot) -> None:
    """Fail closed if delete/reinstall won while capture ran outside the lock."""
    try:
        current = path.lstat()
        current_parent = path.parent.lstat()
    except OSError as error:
        raise StillError(f"{path} was removed while its still was being made") from error
    retained_parent = expected.parent_pin.status()
    actual = _SourceSnapshot(
        current.st_dev,
        current.st_ino,
        stat.S_IFMT(current.st_mode),
        current.st_ctime_ns,
        current.st_size if kind is Kind.VIDEO else None,
        current.st_mtime_ns if kind is Kind.VIDEO else None,
        retained_parent.st_dev,
        retained_parent.st_ino,
        retained_parent.st_ctime_ns if kind is Kind.SCENE else None,
        retained_parent.st_mtime_ns if kind is Kind.SCENE else None,
        expected.descriptor,
        expected.parent_pin,
    )
    valid_type = (
        stat.S_ISDIR(current.st_mode) if kind is Kind.SCENE else stat.S_ISREG(current.st_mode)
    )
    if (
        not valid_type
        or (current_parent.st_dev, current_parent.st_ino)
        != (expected.parent_device, expected.parent_inode)
        or actual != expected
    ):
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
    source = _snapshot_source(video, Kind.VIDEO)
    publication: _SidecarPublication | None = None
    try:
        _require_unchanged_source(video, Kind.VIDEO, source)
        publication = _write_sidecar_publication(video, still, source=source)
        try:
            _require_unchanged_source(video, Kind.VIDEO, source)
        except BaseException:
            publication.rollback()
            raise
        return publication.path
    finally:
        if publication is not None:
            publication.close()
        source.close()


@dataclass(slots=True)
class _SidecarPublication:
    """A newly published sidecar generation which can be rolled back exactly."""

    path: Path
    access_path: Path
    pin: file_io.PinnedPath | None = field(default=None, repr=False)

    def rollback(self) -> None:
        pin = self.pin
        if pin is None:
            return
        try:
            file_io.discard_regular_if_same(
                self.access_path,
                expected_identity=pin.identity,
                expected_fingerprint=pin.fingerprint,
                pinned_source=pin,
                logical_retained_parent=self.path.parent,
            )
        finally:
            pin.close()
            self.pin = None

    def close(self) -> None:
        if self.pin is not None:
            self.pin.close()
            self.pin = None


def _write_sidecar_publication(
    video: Path,
    still: Path,
    *,
    source: _SourceSnapshot | None = None,
) -> _SidecarPublication:
    """Publish a sidecar and retain rollback authority only when it was new."""
    path = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    access_path = (
        Path("/proc/self/fd") / str(source.parent_pin.descriptor) / path.name
        if source is not None
        else path
    )
    encoded = (json.dumps({pairing.SIDECAR_STILL_KEY: str(still)}, indent=2) + "\n").encode()
    temporary_pin: file_io.PinnedPath | None = None
    public_pin: file_io.PinnedPath | None = None
    publication: _SidecarPublication | None = None
    committed = False
    handed_off = False
    try:
        existing = file_io.read_regular_bytes(access_path, pairing.MAX_SIDECAR_BYTES)
        if existing is not None:
            if existing == encoded:
                return _SidecarPublication(path, access_path)
            raise StillError(
                f"could not write {path}: an existing pairing record was left untouched"
            )
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=access_path.parent)
        temporary = Path(name)
        temporary_pin = file_io.PinnedPath(temporary, descriptor, stat.S_IFREG)
        created = temporary_pin.status()
        named = temporary.lstat()
        if not file_io._same_pinned_entry(created, named, stat.S_IFREG):
            raise file_io.PathChangedError(
                f"the private pairing candidate {temporary} changed after creation"
            )
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(f"could not finish writing pairing candidate {temporary}")
            remaining = remaining[written:]
        os.fsync(descriptor)
        candidate_fingerprint = temporary_pin.fingerprint
        if (
            file_io.read_pinned_regular_bytes(
                temporary_pin,
                pairing.MAX_SIDECAR_BYTES,
                expected_fingerprint=candidate_fingerprint,
            )
            != encoded
        ):
            raise StillError(f"could not retain the pairing candidate for {path}")
        file_io.atomic_move_no_replace(
            temporary,
            access_path,
            expected_identity=candidate_fingerprint[:2],
            expected_fingerprint=candidate_fingerprint,
            pinned_source=temporary_pin,
        )
        committed = True
        public_pin = file_io.PinnedPath(
            access_path,
            os.dup(temporary_pin.descriptor),
            stat.S_IFREG,
        )
        publication = _SidecarPublication(path, access_path, public_pin)
        paths.fsync_directory(access_path.parent)
        published = public_pin.status()
        public_named = access_path.lstat()
        if not file_io._same_pinned_entry(published, public_named, stat.S_IFREG):
            raise file_io.PathChangedError(
                f"the published pairing record {path} changed before it could be retained"
            )
        if source is not None:
            _require_unchanged_source(video, Kind.VIDEO, source)
            logical_named = path.lstat()
            if not file_io._same_pinned_entry(published, logical_named, stat.S_IFREG):
                raise file_io.PathChangedError(
                    f"the public pairing record {path} changed parent generation"
                )
        temporary_pin.close()
        temporary_pin = None
        handed_off = True
        return publication
    except FileExistsError as error:
        raise StillError(
            f"could not write {path}: an existing pairing record was left untouched"
        ) from error
    except StillError:
        raise
    except OSError as error:
        raise StillError(f"could not write {path}: {error.strerror or error}") from error
    finally:
        try:
            if temporary_pin is not None:
                try:
                    if not committed:
                        with contextlib.suppress(OSError, ValueError):
                            file_io.discard_regular_if_same(
                                temporary_pin.path,
                                expected_identity=temporary_pin.identity,
                                expected_fingerprint=temporary_pin.fingerprint,
                                pinned_source=temporary_pin,
                            )
                finally:
                    temporary_pin.close()
        finally:
            if publication is not None and not handed_off:
                # Any error after the atomic move still owns exact rollback
                # authority. Do not strand a sidecar merely because its fsync
                # or final visibility check failed.
                publication.rollback()
            elif public_pin is not None and publication is None:
                public_pin.close()


def _private_image_temporary(
    target: Path,
    target_context: file_io.PinnedDirectoryContext,
) -> _ImageTemporary:
    """Atomically create and retain the exact output an external writer receives."""
    output_pin: file_io.PinnedPath | None = None
    try:
        output_descriptor, output_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp.png",
            dir=target_context.directory_anchor,
        )
        access_path = Path(output_name)
        output_pin = file_io.PinnedPath(access_path, output_descriptor, stat.S_IFREG)
        created_output = output_pin.status()
        named_output = access_path.lstat()
        if not file_io._same_pinned_entry(created_output, named_output, stat.S_IFREG):
            raise file_io.PathChangedError(
                f"the private still output {access_path} changed after creation"
            )
        logical_path = target_context.directory / access_path.name
        return _ImageTemporary(
            path=access_path,
            logical_path=logical_path,
            pin=output_pin,
        )
    except BaseException:
        if output_pin is not None:
            try:
                with contextlib.suppress(OSError, ValueError):
                    file_io.discard_regular_if_same(
                        output_pin.path,
                        expected_identity=output_pin.identity,
                        expected_fingerprint=output_pin.fingerprint,
                        pinned_source=output_pin,
                        retained_parent=access_path.parent,
                        logical_retained_parent=target_context.directory,
                    )
            finally:
                output_pin.close()
        raise


def _pin_target_directory(
    root: Path,
    target: Path,
) -> file_io.PinnedDirectoryContext:
    """Retain the no-symlink root and Automatic Stills directory."""
    absolute_root = root.absolute()
    absolute_directory = target.parent.absolute()
    try:
        root_status = absolute_root.lstat()
        directory_status = absolute_directory.lstat()
        if not stat.S_ISDIR(root_status.st_mode) or not stat.S_ISDIR(directory_status.st_mode):
            raise OSError("automatic-still root contains a non-directory entry")
        return file_io.pin_directory_beneath(
            absolute_root,
            absolute_directory,
            expected_root_identity=(root_status.st_dev, root_status.st_ino),
            expected_directory_identity=(directory_status.st_dev, directory_status.st_ino),
        )
    except (OSError, ValueError) as error:
        raise StillError(
            f"could not safely retain automatic-still directory {target.parent}: {error}"
        ) from error


def _pin_existing_target(
    target: Path,
    access: Path,
) -> _ExistingTarget | None:
    """Freeze the app-reserved target generation observed before rendering."""
    try:
        pin = file_io.pin_regular_path(access)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StillError(f"could not safely retain existing still {target}: {error}") from error
    try:
        return _ExistingTarget(pin, pin.fingerprint)
    except BaseException:
        pin.close()
        raise


def _publish_image(
    temporary: _ImageTemporary,
    target: Path,
    target_access: Path,
    target_context: file_io.PinnedDirectoryContext,
    existing: _ExistingTarget | None,
) -> _ImagePublication:
    """Publish while retaining rollback authority over new and prior stills."""
    fingerprint = temporary.retain_rendered()
    claim: file_io.ClaimedPath | None = None
    media_committed = False
    publication: _ImagePublication | None = None
    handed_off = False
    try:
        target_context.verify_public()
        if existing is not None:
            claim = file_io.claim_for_deletion(
                target_access,
                expected_identity=existing.fingerprint[:2],
                expected_fingerprint=existing.fingerprint,
                pinned_source=existing.pin,
                logical_path=target,
            )
        try:
            assert temporary.pin is not None
            file_io.atomic_move_no_replace(
                temporary.path,
                target_access,
                expected_identity=fingerprint[:2],
                expected_fingerprint=fingerprint,
                pinned_source=temporary.pin,
            )
            media_committed = True
            temporary.mark_published()
        except BaseException as error:
            if claim is not None:
                try:
                    restored = claim.restore()
                except OSError as restore_error:
                    restore_error.add_note(f"publication also failed: {error}")
                    raise
                if not restored:
                    raise file_io.PathChangedError(
                        f"a later still won {target}; the prior app-reserved generation "
                        f"was preserved at {claim.path}",
                        preserved_path=claim.path,
                    ) from error
            raise
        assert temporary.pin is not None
        public_pin = file_io.PinnedPath(
            target_access,
            os.dup(temporary.pin.descriptor),
            stat.S_IFREG,
        )
        committed_fingerprint = public_pin.fingerprint
        publication = _ImagePublication(
            target,
            target_access,
            target_context,
            committed_fingerprint,
            public_pin,
            claim,
        )
        paths.fsync_directory(target_access.parent)
        publication.verify()
        handed_off = True
        return publication
    finally:
        if not handed_off:
            if publication is not None:
                try:
                    publication.rollback()
                finally:
                    publication.close()
            elif claim is not None:
                claim.close()
        # A post-commit sync/retirement failure must never make cleanup try to
        # discard the now-public exact generation through its old temp name.
        if media_committed:
            temporary.mark_published()


def _require_public_target(
    target_context: file_io.PinnedDirectoryContext,
    target: Path,
    target_access: Path,
    expected_fingerprint: file_io.FileFingerprint,
) -> None:
    """Require the logical still name to expose the exact committed generation."""
    target_context.verify_public()
    pin = file_io.pin_regular_path(
        target_access,
        expected_identity=expected_fingerprint[:2],
        expected_fingerprint=expected_fingerprint,
    )
    try:
        named = target.lstat()
        try:
            named_fingerprint = file_io.file_fingerprint(named)
        except ValueError as error:
            raise file_io.PathChangedError(
                f"the public automatic-still target {target} is no longer a regular file"
            ) from error
        if named_fingerprint != pin.fingerprint:
            raise file_io.PathChangedError(
                f"the public automatic-still target {target} changed generation"
            )
        target_context.verify_public()
    finally:
        pin.close()


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
        "-c:v",
        "png",
        "-f",
        "image2",
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
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise StillError(f"could not create {target.parent}: {error.strerror or error}") from error
    target_context = _pin_target_directory(root, target)
    target_access = target_context.child(target.name)
    try:
        if not force:
            # Reusing a target can still publish its sidecar. It therefore has
            # the same lifecycle boundary as a newly rendered frame.
            try:
                with _source_lifecycle_lock(video, Kind.VIDEO):
                    _require_unchanged_source(video, Kind.VIDEO, source)
                    if _nonempty_regular(target_access):
                        reused = file_io.regular_file_fingerprint(target_access)
                        _require_public_target(
                            target_context,
                            target,
                            target_access,
                            reused,
                        )
                        reused_sidecar_publication = _record_beside_guarded(
                            video,
                            target,
                            root,
                            target_context,
                            target_access,
                            reused,
                            source,
                        )
                        try:
                            _require_unchanged_source(video, Kind.VIDEO, source)
                            _require_public_target(
                                target_context,
                                target,
                                target_access,
                                reused,
                            )
                        except BaseException:
                            if reused_sidecar_publication is not None:
                                reused_sidecar_publication.rollback()
                            raise
                        finally:
                            if reused_sidecar_publication is not None:
                                reused_sidecar_publication.close()
                        return target
            except StillError:
                raise
            except OSError as error:
                raise StillError(
                    f"could not safely publish a still for {video.name}: {error}"
                ) from error

        existing = _pin_existing_target(target, target_access)
        # A half-written still is worse than none: `pairing` would find it, and
        # the user would get a torn frame as their wallpaper.
        try:
            temporary = _private_image_temporary(target, target_context)
        except OSError as error:
            if existing is not None:
                existing.close()
            raise StillError(
                f"could not create a temporary still in {target.parent}: {error}"
            ) from error
        try:
            complaint = _run(
                source.writer_path,
                temporary.writer_path,
                SEEK_SECONDS,
                processes=processes,
            )
            if complaint:
                # The seek landing past the end of a short loop is the ordinary
                # way this fails, and the first frame is a fine answer for a
                # clip that short.
                complaint = _run(
                    source.writer_path,
                    temporary.writer_path,
                    FALLBACK_SEEK_SECONDS,
                    processes=processes,
                )
            if complaint:
                raise StillError(f"ffmpeg could not take a still from {video.name}: {complaint}")
            # Only the short commit is serialized; the expensive renderer wrote
            # the descriptor-backed hidden output above.
            with _source_lifecycle_lock(video, Kind.VIDEO):
                _require_unchanged_source(video, Kind.VIDEO, source)
                image_publication = _publish_image(
                    temporary,
                    target,
                    target_access,
                    target_context,
                    existing,
                )
                sidecar_publication: _SidecarPublication | None = None
                try:
                    image_publication.verify()
                    sidecar_publication = _record_beside_guarded(
                        video,
                        target,
                        root,
                        target_context,
                        target_access,
                        image_publication.fingerprint,
                        source,
                    )
                    _require_unchanged_source(video, Kind.VIDEO, source)
                    image_publication.verify()
                    image_publication.commit()
                except BaseException:
                    try:
                        if sidecar_publication is not None:
                            sidecar_publication.rollback()
                    finally:
                        image_publication.rollback()
                    raise
                else:
                    if sidecar_publication is not None:
                        sidecar_publication.close()
                finally:
                    image_publication.close()
        except StillError:
            raise
        except OSError as error:
            raise StillError(f"could not write {target}: {error.strerror or error}") from error
        finally:
            temporary.close()
            if existing is not None:
                existing.close()
        return target
    finally:
        target_context.close()


def _record_beside(
    video: Path,
    still: Path,
    root: Path,
    source: _SourceSnapshot,
) -> _SidecarPublication | None:
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
        return None
    return _write_sidecar_publication(video, still, source=source)


def _record_beside_guarded(
    video: Path,
    still: Path,
    root: Path,
    target_context: file_io.PinnedDirectoryContext,
    target_access: Path,
    expected_fingerprint: file_io.FileFingerprint,
    source: _SourceSnapshot,
) -> _SidecarPublication | None:
    """Publish the optional sidecar only while source and still stay exact."""
    _require_unchanged_source(video, Kind.VIDEO, source)
    publication = _record_beside(video, still, root, source)
    try:
        _require_unchanged_source(video, Kind.VIDEO, source)
        _require_public_target(
            target_context,
            still,
            target_access,
            expected_fingerprint,
        )
    except BaseException as publication_error:
        if publication is not None:
            try:
                publication.rollback()
            except OSError as rollback_error:
                rollback_error.add_note(
                    f"the source or public still also changed during sidecar publication: "
                    f"{publication_error}"
                )
                raise
        raise
    return publication


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
            root=root,
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
    root: Path,
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
    target_context = _pin_target_directory(root, target)
    target_access = target_context.child(target.name)
    try:
        existing = _pin_existing_target(target, target_access)
    except BaseException:
        target_context.close()
        raise
    try:
        temporary = _private_image_temporary(target, target_context)
    except OSError as error:
        if existing is not None:
            existing.close()
        target_context.close()
        raise StillError(
            f"could not create a temporary still in {target.parent}: {error}"
        ) from error
    try:
        if processes is None:
            scenes.screenshot(
                item.scene,
                temporary.writer_path,
                size=size,
                prepared_output=True,
            )
        else:
            scenes.screenshot(
                item.scene,
                temporary.writer_path,
                size=size,
                processes=processes,
                prepared_output=True,
            )
        with _source_lifecycle_lock(item.path, Kind.SCENE, item.scene):
            _require_unchanged_source(item.path, Kind.SCENE, source)
            image_publication = _publish_image(
                temporary,
                target,
                target_access,
                target_context,
                existing,
            )
            try:
                _require_unchanged_source(item.path, Kind.SCENE, source)
                image_publication.verify()
                image_publication.commit()
            except BaseException:
                image_publication.rollback()
                raise
            finally:
                image_publication.close()
        return target
    except StillError:
        raise
    except scenes.SceneError as error:
        raise StillError(str(error)) from error
    except OSError as error:
        raise StillError(f"could not replace {target}: {error.strerror or error}") from error
    finally:
        temporary.close()
        if existing is not None:
            existing.close()
        target_context.close()


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
