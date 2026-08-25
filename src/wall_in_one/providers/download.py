"""Where a download lands, and how it gets there without eating anything.

Two files prove a wallpaper is ours to delete, and `library.scan` demands
both: a **directory marker** saying we created the directory, and a **per-file
sidecar** saying we downloaded that particular file. A marker alone is not
enough, which is what keeps a file the user dropped into a managed directory
by hand out of reach of the delete button.

The names here are therefore not free -- they have to be the provider
provenance names `library.scan` already looks for. Predecessor records remain
useful origin metadata, but only current generation-bound records grant
deletion authority. Pairing metadata is separate and never grants it.

Installation atomically renames staged temporaries without replacement.
Media is the commit point. Its exact post-rename generation and digest are
then bound into the sidecar before that authority is published. Process death
between those steps leaves visible but conservatively user-owned media, never
authority which can attach to a later lifecycle. Each regular temporary keeps
a live O_PATH pin and full generation fingerprint through publication and
failure cleanup. A published pathname is never unlinked for rollback.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from wall_in_one import file_io, paths
from wall_in_one.library import stills
from wall_in_one.providers.base import CancellationProbe, ProviderError, refuse_cancellation


def _close_descriptor_preserving_error(descriptor: int, purpose: str) -> None:
    """Close one capability without replacing an exception already in flight."""
    active_error = sys.exception()
    try:
        os.close(descriptor)
    except OSError as close_error:
        if active_error is not None:
            active_error.add_note(f"also could not close {purpose}: {close_error}")
            return
        raise


def _fdopen_owned(descriptor: int, mode: str) -> IO[Any]:
    """Consume one creation descriptor even when ``fdopen`` itself fails."""
    try:
        return os.fdopen(descriptor, mode)
    except BaseException:
        _close_descriptor_preserving_error(descriptor, "a provider staging file")
        raise


#: Everything this app downloads lives under one directory in the user's
#: wallpaper root, so a whole install is one directory to inspect or delete.
MANAGED_PARENT: Final = "Wall-in-One"

#: A sidecar is a few hundred bytes; this only stops a bug writing a novel.
MAX_SIDECAR_BYTES: Final = 64 * 1024

#: Enough distinct names that a collision means something is wrong, few enough
#: that the loop terminates promptly.
MAX_NAME_ATTEMPTS: Final = 10_000

#: Typed hidden names make recovery conservative: only files this subsystem
#: could have created are ever swept after a hard kill.
MEDIA_STAGING_PREFIX: Final = ".wall-in-one-media-stage-"
SIDECAR_STAGING_PREFIX: Final = ".wall-in-one-sidecar-stage-"
MARKER_STAGING_PREFIX: Final = ".wall-in-one-marker-stage-"
LEGACY_STAGING_PREFIXES: Final[tuple[str, ...]] = (
    ".wall-in-one-staged-",
    ".wall-in-one-tmp-",
)

#: A live transfer may legitimately take minutes. Recovery waits a day so it
#: cannot race another process that is still validating a large download.
STAGING_MAX_AGE_SECONDS: Final = 24 * 60 * 60
TEMPORARY_SUFFIX_LENGTH: Final = 8
TEMPORARY_SUFFIX_CHARACTERS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


@dataclass(slots=True)
class _DirectoryAnchor:
    """A retained, no-follow authority for one managed provider directory."""

    logical: Path
    descriptor: int = field(repr=False)
    identity: file_io.PathIdentity
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def proc_path(self) -> Path:
        if self._closed:
            raise OSError(f"the managed-directory capability for {self.logical} is closed")
        return Path(f"/proc/self/fd/{self.descriptor}")

    def entry(self, path: Path) -> Path:
        """Return a descriptor-anchored spelling of one direct logical child."""
        logical = Path(os.fspath(path))
        if logical.parent != self.logical or logical.name in {"", ".", ".."}:
            raise ProviderError(
                "invalid-path", f"provider path is not a direct child of {self.logical}: {logical}"
            )
        return self.proc_path / logical.name

    def verify_public(self) -> None:
        """Require the logical name to still resolve to this retained directory."""
        try:
            retained = os.fstat(self.descriptor)
            named = self.logical.lstat()
        except OSError as error:
            raise ProviderError(
                "invalid-path",
                f"managed provider directory changed after validation: {self.logical}",
            ) from error
        if (
            not stat.S_ISDIR(retained.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (retained.st_dev, retained.st_ino) != self.identity
            or (named.st_dev, named.st_ino) != self.identity
        ):
            raise ProviderError(
                "invalid-path",
                f"managed provider directory changed after validation: {self.logical}",
            )

    def close(self) -> None:
        if not self._closed:
            descriptor = self.descriptor
            self._closed = True
            self.descriptor = -1
            _close_descriptor_preserving_error(
                descriptor,
                f"the managed-directory capability for {self.logical}",
            )

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            with contextlib.suppress(OSError):
                self.close()


class _AnchoredPath(Path):
    """A logical Path whose direct children share managed-directory authority."""

    __slots__ = ("_directory_anchor",)
    _directory_anchor: _DirectoryAnchor

    def __new__(
        cls,
        *pathsegments: str | os.PathLike[str],
        directory_anchor: _DirectoryAnchor,
    ) -> _AnchoredPath:
        instance = super().__new__(cls, *pathsegments)
        instance._directory_anchor = directory_anchor
        return instance

    def __init__(
        self,
        *pathsegments: str | os.PathLike[str],
        directory_anchor: _DirectoryAnchor,
    ) -> None:
        del directory_anchor
        super().__init__(*pathsegments)

    def with_segments(self, *pathsegments: str | os.PathLike[str]) -> _AnchoredPath:
        return type(self)(*pathsegments, directory_anchor=self._directory_anchor)


def _anchor_for_directory(directory: Path) -> _DirectoryAnchor | None:
    if not isinstance(directory, _AnchoredPath):
        return None
    anchor = directory._directory_anchor
    if Path(os.fspath(directory)) != anchor.logical:
        return None
    return anchor


def _anchor_for_entry(path: Path) -> _DirectoryAnchor | None:
    if not isinstance(path, _AnchoredPath):
        return None
    anchor = path._directory_anchor
    logical = Path(os.fspath(path))
    return anchor if logical.parent == anchor.logical else None


def _operation_path(
    path: Path,
    anchor: _DirectoryAnchor | None = None,
    *,
    verify_public: bool = False,
) -> Path:
    retained = anchor if anchor is not None else _anchor_for_entry(path)
    if retained is None:
        return Path(os.fspath(path))
    if verify_public:
        retained.verify_public()
    return retained.entry(Path(os.fspath(path)))


def _mkstemp(*, prefix: str, directory: Path) -> tuple[int, Path]:
    """Create a 0600 temporary through retained directory authority when present."""
    anchor = _anchor_for_directory(directory)
    if anchor is None:
        descriptor, name = tempfile.mkstemp(prefix=prefix, dir=directory)
        return descriptor, Path(name)
    anchor.verify_public()
    descriptor, anchored_name = tempfile.mkstemp(prefix=prefix, dir=anchor.proc_path)
    return descriptor, directory / Path(anchored_name).name


@dataclass(slots=True)
class _StagedFile:
    """A transfer's exact staged inode, shared across path-only wrappers.

    MotionBGS deliberately wraps a transport :class:`Transfer` with the
    authorising URL it followed.  That wrapper carries the same ``Path``
    object, so this small identity-keyed registry lets both transfers retain
    one live O_PATH capability without widening the provider-facing API.  The
    strong registry-path references prevent Python from recycling an identity
    key while any handoff alias is registered.
    """

    path: Path
    handoff: Path
    pin: file_io.PinnedPath = field(repr=False)
    fingerprint: file_io.FileFingerprint
    directory_anchor: _DirectoryAnchor | None = field(default=None, repr=False)
    registry_paths: list[Path] = field(default_factory=list, repr=False)
    references: int = 0
    consumed: bool = False
    registered: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class _PinnedReadPath(Path):
    """A public staging pathname whose reads are bound to its retained inode."""

    __slots__ = ("_public_path", "_staged_file")
    _staged_file: _StagedFile
    _public_path: Path

    def __new__(
        cls,
        *pathsegments: str | os.PathLike[str],
        staged_file: _StagedFile,
        public_path: Path,
    ) -> _PinnedReadPath:
        instance = super().__new__(cls, *pathsegments)
        instance._staged_file = staged_file
        instance._public_path = public_path
        return instance

    def __init__(
        self,
        *pathsegments: str | os.PathLike[str],
        staged_file: _StagedFile,
        public_path: Path,
    ) -> None:
        # Consume the capability-only keywords, while still letting Path build
        # its internal parsed representation from the public segments.
        del staged_file, public_path
        super().__init__(*pathsegments)

    def with_segments(self, *pathsegments: str | os.PathLike[str]) -> _PinnedReadPath:
        return type(self)(
            *pathsegments,
            staged_file=self._staged_file,
            public_path=self._public_path,
        )

    def _is_staged_entry(self) -> bool:
        return Path(os.fspath(self)) == self._public_path

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if not self._is_staged_entry():
            return super().stat(follow_symlinks=follow_symlinks)
        staged = self._staged_file
        with staged.lock:
            _verify_staged_file(staged)
            return staged.pin.status()

    def lstat(self) -> os.stat_result:
        # Destructive publication helpers must inspect the public name, not the
        # retained inode used for validation reads.
        return os.lstat(self)

    def open(  # type: ignore[override]
        self,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        if not self._is_staged_entry():
            return super().open(mode, buffering, encoding, errors, newline)
        staged = self._staged_file
        with staged.lock:
            _verify_staged_file(staged)
            # Opening the proc descriptor while holding the same lock used to
            # close the O_PATH pin eliminates descriptor-number reuse. The new
            # stream then owns an independent reference to the exact inode.
            retained = Path(f"/proc/self/fd/{staged.pin.descriptor}")
            return retained.open(mode, buffering, encoding, errors, newline)


_STAGED_FILES_LOCK = threading.Lock()
_STAGED_FILES: dict[int, _StagedFile] = {}


def _registered_staged_file(path: Path) -> _StagedFile | None:
    """Return the owner of this exact Path object while the registry is locked."""
    existing = _STAGED_FILES.get(id(path))
    if existing is None:
        return None
    if not any(alias is path for alias in existing.registry_paths):
        raise RuntimeError("staged-file registry identity collision")
    return existing


def _unregister_staged_file_locked(staged: _StagedFile) -> None:
    """Remove every exact-object alias; the caller holds the registry lock."""
    for alias in staged.registry_paths:
        if _STAGED_FILES.get(id(alias)) is not staged:
            raise RuntimeError("staged-file registry ownership changed unexpectedly")
    for alias in staged.registry_paths:
        del _STAGED_FILES[id(alias)]
    staged.registry_paths.clear()
    staged.registered = False


def _verify_staged_file(staged: _StagedFile) -> None:
    """Bind a registered pathname to its still-live full generation."""
    if staged.consumed:
        raise file_io.PathChangedError(f"the staged capability for {staged.path} was consumed")
    try:
        if staged.directory_anchor is not None:
            staged.directory_anchor.verify_public()
        pinned = staged.pin.status()
        named = _operation_path(staged.path, staged.directory_anchor).lstat()
        pinned_fingerprint = file_io.file_fingerprint(pinned)
        named_fingerprint = file_io.file_fingerprint(named)
    except (OSError, ValueError, ProviderError) as error:
        raise file_io.PathChangedError(
            f"the staged download at {staged.path} is no longer its retained regular file"
        ) from error
    if pinned_fingerprint != staged.fingerprint or named_fingerprint != staged.fingerprint:
        raise file_io.PathChangedError(
            f"the staged download at {staged.path} changed after validation"
        )


def _unregister_staged_file(staged: _StagedFile) -> None:
    """Remove exactly this capability from the handoff registry, once."""
    with _STAGED_FILES_LOCK:
        if not staged.registered:
            return
        _unregister_staged_file_locked(staged)


def _retain_staged_file(
    path: Path,
    *,
    pinned_source: file_io.PinnedPath | None = None,
    expected_fingerprint: file_io.FileFingerprint | None = None,
) -> _StagedFile:
    """Retain one exact staged generation for a Transfer or install borrow.

    A supplied pin is consumed by this function even when the handoff is
    rejected.  Without one, an existing path-object registration is reused;
    otherwise the current full generation is pinned as the caller's final
    observation.  There is never a device/inode-only fallback.
    """
    if (pinned_source is None) != (expected_fingerprint is None):
        if pinned_source is not None:
            pinned_source.close()
        raise ValueError("a staged pin and its full fingerprint must be handed off together")

    with _STAGED_FILES_LOCK:
        existing = _registered_staged_file(path)
        if existing is not None:
            existing.references += 1
    if existing is not None:
        try:
            with existing.lock:
                _verify_staged_file(existing)
                if pinned_source is not None and (
                    pinned_source.path != existing.pin.path
                    or pinned_source.expected_file_type != stat.S_IFREG
                    or pinned_source.fingerprint != expected_fingerprint
                    or pinned_source.fingerprint != existing.fingerprint
                ):
                    raise file_io.PathChangedError(
                        f"duplicate staged handoff for {path} names another generation"
                    )
        except BaseException:
            _release_staged_file(existing)
            if pinned_source is not None:
                pinned_source.close()
            raise
        if pinned_source is not None:
            pinned_source.close()
        return existing

    candidate_pin = pinned_source
    directory_anchor = _anchor_for_entry(path)
    try:
        retained_path = _operation_path(path, directory_anchor, verify_public=True)
        if candidate_pin is None:
            candidate_pin = file_io.pin_regular_path(retained_path)
            expected_fingerprint = candidate_pin.fingerprint
        if (
            candidate_pin.path != retained_path
            or candidate_pin.expected_file_type != stat.S_IFREG
            or candidate_pin.fingerprint != expected_fingerprint
        ):
            raise file_io.PathChangedError(
                f"staged handoff for {path} does not match its retained generation"
            )
        public_path = Path(os.fspath(path))
        candidate = _StagedFile(
            path=public_path,
            handoff=path,
            pin=candidate_pin,
            fingerprint=expected_fingerprint,
            directory_anchor=directory_anchor,
            registry_paths=[path],
        )
        with candidate.lock:
            _verify_staged_file(candidate)
    except BaseException:
        if candidate_pin is not None:
            candidate_pin.close()
        raise

    with _STAGED_FILES_LOCK:
        existing = _registered_staged_file(path)
        if existing is None:
            candidate.references = 1
            candidate.registered = True
            _STAGED_FILES[id(path)] = candidate
            return candidate
        existing.references += 1

    # Another thread registered this exact Path object while we opened the
    # candidate pin.  Collapse only if both capabilities still prove the same
    # full generation; otherwise reject the stale/duplicate handoff.
    try:
        with existing.lock:
            _verify_staged_file(existing)
            if candidate.fingerprint != existing.fingerprint:
                raise file_io.PathChangedError(
                    f"concurrent staged handoff for {path} changed generation"
                )
    except BaseException:
        _release_staged_file(existing)
        candidate.pin.close()
        raise
    candidate.pin.close()
    return existing


def _validation_path(staged: _StagedFile) -> Path:
    """Expose a Path-compatible read seam bound to the retained inode."""
    with staged.lock:
        _verify_staged_file(staged)
        if isinstance(staged.handoff, _PinnedReadPath):
            return staged.handoff
        validation = _PinnedReadPath(
            staged.path,
            staged_file=staged,
            public_path=staged.path,
        )
        with _STAGED_FILES_LOCK:
            if not staged.registered:
                raise file_io.PathChangedError(
                    f"the staged capability for {staged.path} is no longer registered"
                )
            if _registered_staged_file(staged.handoff) is not staged:
                raise RuntimeError("staged-file registry ownership changed unexpectedly")
            if id(validation) in _STAGED_FILES:
                raise RuntimeError("staged validation-path registry identity collision")
            staged.handoff = validation
            staged.registry_paths.append(validation)
            _STAGED_FILES[id(validation)] = staged
        return validation


def _release_staged_file(staged: _StagedFile) -> None:
    """Release one reference, closing an unconsumed last capability."""
    close_pin = False
    with _STAGED_FILES_LOCK:
        if staged.references <= 0:
            raise RuntimeError("staged-file capability released more than once")
        staged.references -= 1
        if staged.references == 0 and staged.registered:
            _unregister_staged_file_locked(staged)
            close_pin = not staged.consumed
    if close_pin:
        with staged.lock:
            staged.pin.close()
            staged.directory_anchor = None


def _consume_staged_file(staged: _StagedFile, *, discard: bool) -> bool:
    """End a capability after commit or an exact private-path cleanup."""
    removed = False
    directory_anchor = staged.directory_anchor
    with staged.lock:
        if staged.consumed:
            return False
        try:
            if discard:
                try:
                    removed = file_io.discard_regular_if_same(
                        _operation_path(staged.path, directory_anchor),
                        expected_identity=staged.fingerprint[:2],
                        expected_fingerprint=staged.fingerprint,
                        pinned_source=staged.pin,
                    )
                except file_io.PathChangedError as error:
                    preserved = error.preserved_path
                    raise ProviderError(
                        "local-io",
                        f"transfer cleanup preserved a changed entry at {preserved or staged.path}",
                    ) from error
        finally:
            # A failed exact claim means the public name changed; the caller's
            # capability must still be retired so it cannot later authorise a
            # different generation. PinnedPath.close() is idempotent when a
            # successful claim already consumed it.
            staged.consumed = True
            staged.pin.close()
            _unregister_staged_file(staged)
            staged.directory_anchor = None
    if removed:
        # The typed staging name remains recoverable if deletion durability is
        # uncertain; no unproven public pathname is touched.
        with contextlib.suppress(OSError):
            paths.fsync_directory(
                directory_anchor.proc_path if directory_anchor is not None else staged.path.parent
            )
    return removed


@dataclass(frozen=True, slots=True)
class ManagedLocation:
    """The naming contract for one provider's downloads."""

    provider: str
    #: Subdirectory of `<root>/Wall-in-One/`.
    directory_name: str
    #: Filename of the directory marker. Must be one that `library.scan` knows.
    marker_name: str
    #: Suffix appended to the media filename for its sidecar. Likewise.
    sidecar_suffix: str
    #: What goes inside the marker. `library.scan._provider_of` reads
    #: ``provider`` first and falls back to ``kind``.
    marker_payload: Mapping[str, object]


MOTIONBGS_LOCATION: Final = ManagedLocation(
    provider="MotionBGS",
    directory_name="MotionBGS",
    marker_name=".wall-in-one-motionbgs-managed.json",
    sidecar_suffix=".motionbgs.json",
    marker_payload={
        "schema": 1,
        "owner": "goober/wall-in-one",
        "provider": "MotionBGS",
        "deletion_authority": "adjacent .motionbgs.json sidecar required",
    },
)

WALLHAVEN_LOCATION: Final = ManagedLocation(
    provider="Wallhaven",
    directory_name="Wallhaven",
    marker_name=".managed-by-wall-in-one-v1.json",
    sidecar_suffix=".wallhaven.json",
    # ``kind`` and ``ownership`` are what the predecessor's marker validator
    # required, so markers already on disk keep validating. ``provider`` is
    # added for `library.scan`, which prefers it and would otherwise report
    # this directory as "wallhaven" in lower case.
    marker_payload={
        "schema": 1,
        "plugin": "goober/wall-in-one",
        "provider": "Wallhaven",
        "kind": "wallhaven",
        "ownership": "managed",
        "deletion_authority": "adjacent .wallhaven.json sidecar required",
    },
)


def encode_sidecar(payload: Mapping[str, object]) -> bytes:
    """Serialise a sidecar, refusing one that has grown implausible."""
    try:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ProviderError("local-io", f"could not encode sidecar: {error}") from error
    encoded = text.encode("utf-8") + b"\n"
    if len(encoded) > MAX_SIDECAR_BYTES:
        raise ProviderError("local-io", "sidecar exceeded its size ceiling")
    return encoded


def _bind_provider_sidecar(
    payload: bytes,
    *,
    destination: Path,
    sidecar_suffix: str,
    staged: _StagedFile,
    media_fingerprint: file_io.FileFingerprint | None = None,
) -> bytes:
    """Bind a provider authority document to one exact media generation.

    Production calls this once before publication to validate the staged
    bytes, then again with the post-rename fingerprint before publishing the
    sidecar.  The final ctime distinguishes a later inode-reuse lifecycle even
    when its pathname, contents, size, and mtime all happen to be identical.

    ``install`` is also a low-level test seam and may receive non-authority
    JSON.  Such payloads are preserved verbatim; only an exact provider
    identity is upgraded (and therefore required to carry its validated size
    and digest).
    """
    expected_provider = {
        ".motionbgs.json": "MotionBGS",
        ".wallhaven.json": "Wallhaven",
    }.get(sidecar_suffix)
    if expected_provider is None:
        return payload
    try:
        document: object = json.loads(payload)
    except ValueError, RecursionError:
        return payload
    if not (
        isinstance(document, dict)
        and type(document.get("schema")) is int
        and document.get("schema") == 1
        and document.get("plugin") == "goober/wall-in-one"
        and document.get("provider") == expected_provider
        and document.get("path") == str(destination)
    ):
        return payload

    recorded_size = document.get("bytes")
    recorded_digest = document.get("sha256")
    if (
        type(recorded_size) is not int
        or recorded_size < 0
        or not isinstance(recorded_digest, str)
        or len(recorded_digest) != 64
        or any(character not in "0123456789abcdef" for character in recorded_digest)
    ):
        raise ProviderError(
            "local-io",
            "provider provenance is missing its validated media size or SHA-256",
        )
    fingerprint = staged.fingerprint if media_fingerprint is None else media_fingerprint
    try:
        actual_size, actual_digest = file_io.hash_pinned_regular(
            staged.pin,
            expected_fingerprint=fingerprint,
        )
    except OSError as error:
        raise ProviderError(
            "local-io",
            f"could not bind provider provenance to the staged download: {error}",
        ) from error
    if (recorded_size, recorded_digest) != (actual_size, actual_digest):
        raise ProviderError(
            "local-io",
            "provider provenance does not match the validated staged download",
        )
    document["media_generation"] = {
        "device": fingerprint[0],
        "inode": fingerprint[1],
        "bytes": fingerprint[2],
        "mtime_ns": fingerprint[3],
        "ctime_ns": fingerprint[4],
    }
    return encode_sidecar(document)


def safe_child(directory: Path, name: str) -> Path:
    """``directory / name``, refusing anything that could leave ``directory``.

    Filenames reach here from provider metadata -- a slug, a remote id -- so
    they are hostile input. Traversal, separators, NULs and the two special
    names are all rejected outright, and then the result is resolved to catch
    the remaining case: ``name`` already existing as a symlink pointing out.
    """
    if not name or len(name) > 255:
        raise ProviderError("invalid-path", "download filename is empty or too long")
    if name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ProviderError("invalid-path", f"download filename is unsafe: {name!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ProviderError("invalid-path", "download filename contains control characters")
    candidate = directory / name
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ProviderError("local-io", f"managed directory is unusable: {error}") from error
    if candidate.resolve().parent != root:
        raise ProviderError("invalid-path", f"download filename escapes its directory: {name!r}")
    return candidate


def managed_directory(root: Path, location: ManagedLocation) -> tuple[Path, Path]:
    """Ensure ``<root>/Wall-in-One/<provider>/`` exists and carries its marker.

    Returns the directory and the marker path. Creating the marker is not
    optional: without it every file inside stays `Ownership.USER` and the app
    would refuse to delete its own downloads.
    """
    if not root.is_absolute():
        raise ProviderError("invalid-path", "download root must be an absolute path")
    if (
        not location.directory_name
        or "/" in location.directory_name
        or "\\" in location.directory_name
    ):
        raise ProviderError("invalid-path", "managed provider directory name is unsafe")
    directory_descriptor: int | None = None
    root_descriptor = _open_real_directory(root, create=False)
    parent = root / MANAGED_PARENT
    try:
        try:
            parent_descriptor = _open_real_directory(
                parent,
                create=True,
                parent_descriptor=root_descriptor,
                child_name=MANAGED_PARENT,
            )
            try:
                directory = parent / location.directory_name
                directory_descriptor = _open_real_directory(
                    directory,
                    create=True,
                    parent_descriptor=parent_descriptor,
                    child_name=location.directory_name,
                )
            finally:
                _close_descriptor_preserving_error(
                    parent_descriptor,
                    f"the managed parent directory {parent}",
                )
        finally:
            _close_descriptor_preserving_error(
                root_descriptor,
                f"the wallpaper root {root}",
            )
    except BaseException:
        if directory_descriptor is not None:
            retained_descriptor = directory_descriptor
            directory_descriptor = None
            _close_descriptor_preserving_error(
                retained_descriptor,
                f"the managed provider directory {parent / location.directory_name}",
            )
        raise

    anchor: _DirectoryAnchor | None = None
    try:
        if directory_descriptor is None:
            raise RuntimeError("managed-directory creation lost its directory descriptor")
        status = os.fstat(directory_descriptor)
        anchor = _DirectoryAnchor(
            logical=directory,
            descriptor=directory_descriptor,
            identity=(status.st_dev, status.st_ino),
        )
        managed = _AnchoredPath(directory, directory_anchor=anchor)
        anchor.verify_public()
        # Validate the ownership marker before recovery is allowed to unlink
        # even an app-shaped staging name. A foreign marker must make this
        # directory inert, not let cleanup run first and complain afterwards.
        marker = _write_marker(managed, location)
        recover_abandoned(managed, location)
        anchor.verify_public()
    except BaseException:
        if anchor is not None:
            directory_descriptor = None
            anchor.close()
        elif directory_descriptor is not None:
            retained_descriptor = directory_descriptor
            directory_descriptor = None
            _close_descriptor_preserving_error(
                retained_descriptor,
                f"the managed provider directory {directory}",
            )
        raise
    directory_descriptor = None
    return managed, Path(os.fspath(marker))


def _open_real_directory(
    directory: Path,
    *,
    create: bool,
    parent_descriptor: int | None = None,
    child_name: str | None = None,
) -> int:
    """Open one exact real directory without following its final component."""
    target: str | os.PathLike[str] = directory if parent_descriptor is None else (child_name or "")
    if parent_descriptor is not None and (not child_name or "/" in child_name):
        raise ValueError("an anchored directory child must be one safe path component")
    try:
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(target, dir_fd=parent_descriptor)
        descriptor = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        raise ProviderError("invalid-path", f"download root does not exist: {directory}") from None
    except OSError as error:
        kind = "invalid-path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "local-io"
        raise ProviderError(
            kind, f"managed path is not a usable real directory: {directory}"
        ) from error

    try:
        opened = os.fstat(descriptor)
        named = os.stat(target, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise ProviderError(
                "invalid-path", f"managed path changed while it was opened: {directory}"
            )
    except BaseException:
        _close_descriptor_preserving_error(descriptor, f"the managed directory {directory}")
        raise
    return descriptor


def recover_abandoned(
    directory: Path,
    location: ManagedLocation,
    *,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Remove old, unmistakably app-owned remnants of interrupted installs.

    Recent files are left alone because another process may still be
    downloading. A predecessor-version final provider sidecar with no adjacent
    media is inert to the scanner and safe to remove once old. The current
    media-first protocol can instead leave visible user-owned media, which
    recovery deliberately never removes. Arbitrary dotfiles, symlinks and
    non-regular files are never touched.
    """
    anchor = _anchor_for_directory(directory)
    if anchor is not None:
        anchor.verify_public()
    marker = directory / location.marker_name
    try:
        raw_marker = file_io.read_regular_bytes(_operation_path(marker, anchor), MAX_SIDECAR_BYTES)
        marker_document: object = json.loads(raw_marker) if raw_marker is not None else None
    except OSError, ValueError, RecursionError:
        marker_document = None
    if not isinstance(marker_document, dict) or not _marker_matches_location(
        marker_document, location
    ):
        raise ProviderError(
            "invalid-path", f"refusing provider cleanup without a valid marker: {marker}"
        )

    cutoff = (time.time() if now is None else now) - STAGING_MAX_AGE_SECONDS
    prefixes = (
        MEDIA_STAGING_PREFIX,
        SIDECAR_STAGING_PREFIX,
        MARKER_STAGING_PREFIX,
        *LEGACY_STAGING_PREFIXES,
    )
    removed: list[Path] = []
    try:
        # Opening the proc alias gives scandir an independent directory-stream
        # offset. Passing the retained descriptor itself would share and exhaust
        # its open-file-description offset across later recovery calls.
        scan_target = anchor.proc_path if anchor is not None else directory
        with os.scandir(scan_target) as iterator:
            for entry in iterator:
                path = directory / entry.name
                retained_path = _operation_path(path, anchor)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                    continue
                staging = _is_owned_staging(entry.name, info, prefixes)
                media: Path | None = None
                orphan_sidecar = False
                if path.name.endswith(location.sidecar_suffix):
                    media = Path(str(path)[: -len(location.sidecar_suffix)])
                    orphan_sidecar = _is_owned_orphan_sidecar(
                        retained_path,
                        location,
                        info.st_size,
                        media_path=_operation_path(media, anchor),
                        logical_path=Path(os.fspath(path)),
                    )
                if not staging and not orphan_sidecar:
                    continue
                try:
                    claim = file_io.claim_for_deletion(
                        retained_path,
                        expected_identity=(info.st_dev, info.st_ino),
                        expected_fingerprint=file_io.file_fingerprint(info),
                    )
                except file_io.PathChangedError as error:
                    if error.preserved_path is not None:
                        raise ProviderError(
                            "local-io",
                            f"provider recovery preserved a changed entry at "
                            f"{error.preserved_path}",
                        ) from error
                    continue
                except OSError:
                    continue

                try:
                    try:
                        claimed_info = claim.path.lstat()
                    except OSError as error:
                        try:
                            restored = claim.restore()
                        except OSError:
                            restored = False
                        preserved = path if restored else claim.path
                        raise ProviderError(
                            "local-io",
                            f"could not revalidate provider staging; entry remains at {preserved}",
                        ) from error
                    valid = claimed_info.st_mtime <= cutoff
                    if staging:
                        valid = valid and _is_owned_staging(path.name, claimed_info, prefixes)
                    if orphan_sidecar and media is not None:
                        valid = valid and _is_owned_orphan_sidecar(
                            claim.path,
                            location,
                            claimed_info.st_size,
                            media_path=_operation_path(media, anchor),
                            logical_path=Path(os.fspath(path)),
                        )
                    if not valid:
                        try:
                            restored = claim.restore()
                        except OSError as error:
                            raise ProviderError(
                                "local-io",
                                f"could not restore an unverified recovery entry at {claim.path}",
                            ) from error
                        if not restored:
                            raise ProviderError(
                                "local-io",
                                "provider recovery left an unverified entry preserved at "
                                f"{claim.path}",
                            )
                        continue
                    try:
                        claim.discard()
                    except OSError as error:
                        try:
                            restored = claim.restore()
                        except OSError:
                            restored = False
                        preserved = path if restored else claim.path
                        raise ProviderError(
                            "local-io",
                            "could not safely clean provider staging; "
                            f"entry remains at {preserved}",
                        ) from error
                    removed.append(Path(os.fspath(path)))
                finally:
                    claim.close()
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not inspect provider staging: {error.strerror or error}"
        ) from error
    if removed:
        try:
            paths.fsync_directory(anchor.proc_path if anchor is not None else directory)
        except OSError as error:
            raise ProviderError(
                "local-io",
                f"cleaned abandoned provider staging but could not persist it: {error}",
            ) from error
    if anchor is not None:
        anchor.verify_public()
    return tuple(removed)


def _is_owned_staging(
    name: str,
    info: os.stat_result,
    prefixes: tuple[str, ...],
) -> bool:
    """Whether an entry has the exact private tempfile shape we generate."""
    suffix: str | None = None
    for prefix in prefixes:
        if name.startswith(prefix):
            suffix = name.removeprefix(prefix)
            break
    return (
        suffix is not None
        and len(suffix) == TEMPORARY_SUFFIX_LENGTH
        and all(character in TEMPORARY_SUFFIX_CHARACTERS for character in suffix)
        and stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
    )


def _is_owned_orphan_sidecar(
    path: Path,
    location: ManagedLocation,
    size: int,
    *,
    media_path: Path | None = None,
    logical_path: Path | None = None,
) -> bool:
    """Prove an orphan is our provider provenance before unlinking it.

    A suffix is only a naming convention, not deletion authority. Recovery
    therefore requires the same identity fields emitted by both providers and
    an exact path binding to the missing adjacent media. This keeps an old
    user-authored ``*.motionbgs.json`` file out of the cleanup sweep.
    """
    named_path = path if logical_path is None else logical_path
    if not named_path.name.endswith(location.sidecar_suffix) or size > MAX_SIDECAR_BYTES:
        return False
    logical_media = Path(str(named_path)[: -len(location.sidecar_suffix)])
    observed_media = logical_media if media_path is None else media_path
    if os.path.lexists(observed_media):
        return False
    try:
        raw = file_io.read_regular_bytes(path, MAX_SIDECAR_BYTES)
        if raw is None:
            return False
        document: object = json.loads(raw)
    except OSError, ValueError, RecursionError:
        return False
    return (
        isinstance(document, dict)
        and type(document.get("schema")) is int
        and document.get("schema") == 1
        and document.get("plugin") == "goober/wall-in-one"
        and document.get("provider") == location.provider
        and document.get("path") == str(logical_media)
    )


def _write_marker(directory: Path, location: ManagedLocation) -> Path:
    marker = directory / location.marker_name
    payload = encode_sidecar(location.marker_payload)
    try:
        existing = file_io.read_regular_bytes(_operation_path(marker), MAX_SIDECAR_BYTES)
    except OSError as error:
        raise ProviderError("invalid-path", f"ownership marker is unsafe: {error}") from error
    if existing is not None:
        try:
            if existing == payload:
                return marker
            document: object = json.loads(existing)
        except ValueError, RecursionError:
            document = None
        if isinstance(document, dict) and _marker_matches_location(document, location):
            return marker
        raise ProviderError(
            "conflict", f"ownership marker is not one this provider recognises: {marker}"
        )
    try:
        _atomic_write(marker, payload, prefix=MARKER_STAGING_PREFIX)
    except ProviderError as error:
        if error.kind != "conflict":
            raise
        # Two first downloads may publish the same no-replace marker. The
        # loser converges only after re-reading and validating the winner.
        raced: bytes | None = None
        try:
            raced = file_io.read_regular_bytes(_operation_path(marker), MAX_SIDECAR_BYTES)
            document = json.loads(raced) if raced is not None else None
        except OSError, ValueError, RecursionError:
            document = None
        if raced == payload or (
            isinstance(document, dict) and _marker_matches_location(document, location)
        ):
            return marker
        raise
    return marker


def _marker_matches_location(document: Mapping[str, object], location: ManagedLocation) -> bool:
    """Current marker, plus the one exact predecessor shape we shipped."""
    if type(document.get("schema")) is not int or document.get("schema") != 1:
        return False
    if location.provider == "MotionBGS" and location.sidecar_suffix == ".motionbgs.json":
        return (
            document.get("plugin", document.get("owner")) == "goober/wall-in-one"
            and document.get("provider", "MotionBGS") == "MotionBGS"
        )
    if location.provider == "Wallhaven" and location.sidecar_suffix == ".wallhaven.json":
        return (
            document.get("kind") == "wallhaven"
            and document.get("ownership") == "managed"
            and document.get("plugin", "goober/wall-in-one") == "goober/wall-in-one"
            and document.get("provider", "Wallhaven") == "Wallhaven"
        )
    return False


def unique_destination(
    directory: Path,
    stem: str,
    extension: str,
    sidecar_suffix: str,
    *,
    cancelled: CancellationProbe | None = None,
) -> Path:
    """First free ``<stem>.<ext>`` in ``directory``, counting up on collisions.

    A name is only free when the media file *and* its sidecar are both absent:
    a stray sidecar with no media means an interrupted install, and reusing
    that name would attach the wrong provenance to a new download.
    """
    for attempt in range(MAX_NAME_ATTEMPTS):
        refuse_cancellation(cancelled)
        name = f"{stem}{extension}" if attempt == 0 else f"{stem}-{attempt}{extension}"
        candidate = safe_child(directory, name)
        if not os.path.lexists(candidate) and not os.path.lexists(str(candidate) + sidecar_suffix):
            return candidate
    raise ProviderError("conflict", f"could not allocate a free name for {stem}")


def install(
    staged: Path,
    destination: Path,
    sidecar_suffix: str,
    sidecar_payload: bytes,
    *,
    cancelled: CancellationProbe | None = None,
) -> tuple[Path, Path]:
    """Move ``staged`` into place next to a freshly written sidecar.

    Both moves are no-replace. The media move is the irreversible commit
    point; its post-rename fingerprint is then bound into the sidecar before
    that authority is published. A hard kill after the media commit can leave
    a visible but deliberately user-owned file, never deletion authority which
    can attach to another lifecycle through inode reuse.
    Once either final pathname has been moved this function will not unlink it
    for rollback: a concurrent local actor could have replaced the pathname,
    and check-then-unlink cannot prove inode ownership atomically. A failure
    after the media move is reported as a committed outcome with unknown final
    durability rather than pretending the download did not land.
    On a pre-commit failure, ownership of ``staged`` remains with the caller:
    a :class:`Transfer` discards it on context exit, while a bare app-shaped
    staging path remains eligible for the marker-gated age-bounded recovery
    sweep. The install borrow itself always releases its descriptor.
    ``staged`` must already be in ``destination``'s directory -- it is, because
    the transport streams downloads into the directory they are destined for,
    which is also what makes the atomic rename same-filesystem by construction.
    """
    refuse_cancellation(cancelled)
    directory = destination.parent
    if staged.parent != directory:
        raise ProviderError("invalid-path", "staged download is not in its destination directory")
    destination_anchor = _anchor_for_directory(directory)
    if destination_anchor is not None:
        destination_anchor.verify_public()
    try:
        staged_file = _retain_staged_file(staged)
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not retain staged download: {error.strerror or error}"
        ) from error
    sidecar_destination = destination.with_name(destination.name + sidecar_suffix)
    media_committed = False
    try:
        # Serialise install/discard over the shared Transfer capability.  The
        # lock is re-entrant because commit consumption unregisters the lease.
        with staged_file.lock, stills.media_path_lifecycle_lock(destination):
            _verify_staged_file(staged_file)
            if destination_anchor is not None:
                _bind_staged_directory(staged_file, destination_anchor)
            # Validate the production provider document and its exact staged
            # bytes before the irreversible media move. Its pre-rename
            # generation is deliberately not published as authority.
            validated_sidecar_payload = _bind_provider_sidecar(
                sidecar_payload,
                destination=destination,
                sidecar_suffix=sidecar_suffix,
                staged=staged_file,
            )
            refuse_cancellation(cancelled)
            file_io.atomic_move_no_replace(
                _operation_path(staged_file.path, staged_file.directory_anchor),
                _operation_path(destination, destination_anchor),
                expected_identity=staged_file.fingerprint[:2],
                expected_fingerprint=staged_file.fingerprint,
                pinned_source=staged_file.pin,
            )
            media_committed = True
            paths.fsync_directory(
                destination_anchor.proc_path if destination_anchor is not None else directory
            )

            published_fingerprint = staged_file.pin.fingerprint
            try:
                named_fingerprint = file_io.regular_file_fingerprint(
                    _operation_path(destination, destination_anchor)
                )
            except (OSError, ValueError) as error:
                raise file_io.PathChangedError(
                    f"the published media at {destination} could not be revalidated"
                ) from error
            if named_fingerprint != published_fingerprint:
                raise file_io.PathChangedError(
                    f"the published media at {destination} changed before provenance publication"
                )
            sidecar_payload = _bind_provider_sidecar(
                validated_sidecar_payload,
                destination=destination,
                sidecar_suffix=sidecar_suffix,
                staged=staged_file,
                media_fingerprint=published_fingerprint,
            )
            descriptor, sidecar_temporary = _mkstemp(
                prefix=SIDECAR_STAGING_PREFIX,
                directory=directory,
            )
            sidecar_pin = _pin_created_temporary(descriptor, sidecar_temporary)
            sidecar_descriptor: int | None = descriptor
            sidecar_fingerprint: file_io.FileFingerprint | None = None
            try:
                sidecar_fingerprint = sidecar_pin.fingerprint
                owned_descriptor = sidecar_descriptor
                sidecar_descriptor = None
                if owned_descriptor is None:
                    raise RuntimeError("sidecar creation descriptor was already consumed")
                with _fdopen_owned(owned_descriptor, "wb") as sink:
                    sink.write(sidecar_payload)
                    sink.flush()
                    os.fsync(sink.fileno())
                sidecar_fingerprint = sidecar_pin.fingerprint
                try:
                    current_media_fingerprint = file_io.regular_file_fingerprint(
                        _operation_path(destination, destination_anchor)
                    )
                except (OSError, ValueError) as error:
                    raise file_io.PathChangedError(
                        f"the published media at {destination} could not be revalidated"
                    ) from error
                if current_media_fingerprint != published_fingerprint:
                    raise file_io.PathChangedError(
                        f"the published media at {destination} changed before provenance "
                        "publication"
                    )
                # No cancellation boundary is allowed after media commit: make
                # the exact authority durable whenever the process can still
                # do so. A conflict leaves the media visible but unmanaged.
                file_io.atomic_move_no_replace(
                    _operation_path(sidecar_temporary),
                    _operation_path(sidecar_destination),
                    expected_identity=sidecar_fingerprint[:2],
                    expected_fingerprint=sidecar_fingerprint,
                    pinned_source=sidecar_pin,
                )
                paths.fsync_directory(
                    destination_anchor.proc_path if destination_anchor is not None else directory
                )
            finally:
                try:
                    if sidecar_descriptor is not None:
                        _close_descriptor_preserving_error(
                            sidecar_descriptor,
                            f"the sidecar staging file {sidecar_temporary}",
                        )
                finally:
                    _discard_pinned_temporary(
                        sidecar_temporary,
                        sidecar_pin,
                        fallback=sidecar_fingerprint,
                    )
    except file_io.PathChangedError as error:
        preserved = error.preserved_path
        if media_committed:
            raise ProviderError(
                "local-io",
                "download reached its irreversible media commit, but exact provenance could "
                "not be published; the file remains visible and conservatively user-owned: "
                f"{error}",
            ) from error
        raise ProviderError(
            "local-io",
            f"provider publication observed a changed entry"
            f"{f' preserved at {preserved}' if preserved is not None else ''}: {error}",
        ) from error
    except ProviderError as error:
        if media_committed:
            raise ProviderError(
                "local-io",
                "download reached its irreversible media commit, but exact provenance could "
                "not be published; the file remains visible and conservatively user-owned: "
                f"{error}",
            ) from error
        raise
    except FileExistsError as error:
        if media_committed:
            raise ProviderError(
                "local-io",
                "download reached its irreversible media commit, but a later local error "
                "left the committed outcome and final durability unknown; inspect the library "
                f"before retrying: {error.strerror or error}",
            ) from error
        raise ProviderError(
            "conflict", f"{error.filename} appeared before it could be installed"
        ) from error
    except OSError as error:
        if media_committed:
            raise ProviderError(
                "local-io",
                "download reached its irreversible media commit, but a later local error "
                "left the committed outcome and final durability unknown; inspect the library "
                f"before retrying: {error.strerror or error}",
            ) from error
        raise ProviderError(
            "local-io", f"could not install download: {error.strerror or error}"
        ) from error
    finally:
        if media_committed:
            _consume_staged_file(staged_file, discard=False)
        _release_staged_file(staged_file)
    if destination_anchor is not None:
        try:
            destination_anchor.verify_public()
        except ProviderError as error:
            raise ProviderError(
                "local-io",
                "download reached its irreversible media commit, but the managed directory "
                "moved before the logical result could be verified; inspect the library "
                f"before retrying: {error}",
            ) from error
    return Path(os.fspath(destination)), Path(os.fspath(sidecar_destination))


def _bind_staged_directory(staged: _StagedFile, anchor: _DirectoryAnchor) -> None:
    """Upgrade a path-only transfer to retained managed-directory authority."""
    if staged.directory_anchor is anchor:
        return
    if staged.path.parent != anchor.logical:
        raise ProviderError(
            "invalid-path", f"staged download is not beneath retained directory {anchor.logical}"
        )
    if staged.directory_anchor is not None and staged.directory_anchor.identity != anchor.identity:
        raise file_io.PathChangedError(
            f"staged download and destination name different managed directories: {staged.path}"
        )
    anchor.verify_public()
    retained_path = anchor.entry(staged.path)
    replacement_pin = file_io.pin_regular_path(
        retained_path,
        expected_identity=staged.fingerprint[:2],
        expected_fingerprint=staged.fingerprint,
    )
    staged.pin.close()
    staged.pin = replacement_pin
    staged.directory_anchor = anchor
    _verify_staged_file(staged)


def _pin_created_temporary(descriptor: int, path: Path) -> file_io.PinnedPath:
    """Pin a mkstemp inode before its creation descriptor can be released."""
    fingerprint: file_io.FileFingerprint | None = None
    try:
        status = os.fstat(descriptor)
        fingerprint = file_io.file_fingerprint(status)
        return file_io.pin_regular_path(
            _operation_path(path, verify_public=True),
            expected_identity=fingerprint[:2],
            expected_fingerprint=fingerprint,
        )
    except BaseException:
        try:
            if fingerprint is not None:
                _discard_owned(path, expected_fingerprint=fingerprint)
        finally:
            _close_descriptor_preserving_error(
                descriptor,
                f"the provider creation descriptor for {path}",
            )
        raise


def _discard_pinned_temporary(
    path: Path,
    pinned: file_io.PinnedPath,
    *,
    fallback: file_io.FileFingerprint | None,
) -> None:
    """Retire one exact temporary without replacing an active primary error."""
    active_error = sys.exception()
    fingerprint = fallback
    pin_handed_off = False
    try:
        try:
            fingerprint = file_io.file_fingerprint(pinned.status())
        except OSError, ValueError:
            if fingerprint is None:
                raise
        pin_handed_off = True
        _discard_owned(
            path,
            expected_fingerprint=fingerprint,
            pinned_source=pinned,
        )
    except BaseException as cleanup_error:
        if not pin_handed_off:
            try:
                pinned.close()
            except OSError as close_error:
                cleanup_error.add_note(f"also could not close the temporary pin: {close_error}")
        if active_error is not None:
            active_error.add_note(
                f"also could not retire provider temporary {path}: {cleanup_error}"
            )
            return
        raise


def _discard_owned(
    path: Path,
    *,
    expected_fingerprint: file_io.FileFingerprint,
    pinned_source: file_io.PinnedPath | None = None,
) -> None:
    """Remove one proven regular temporary and release its retained pin."""
    try:
        removed = file_io.discard_regular_if_same(
            _operation_path(path),
            expected_identity=expected_fingerprint[:2],
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
        )
    except file_io.PathChangedError as error:
        preserved = error.preserved_path
        raise ProviderError(
            "local-io",
            f"provider cleanup preserved a changed entry at {preserved or path}",
        ) from error
    finally:
        if pinned_source is not None:
            pinned_source.close()
    if removed:
        try:
            anchor = _anchor_for_entry(path)
            paths.fsync_directory(anchor.proc_path if anchor is not None else path.parent)
        except OSError:
            # Recovery recognises the typed staging name or orphan sidecar later.
            return


def _atomic_write(destination: Path, payload: bytes, *, prefix: str) -> None:
    """Write ``payload`` to ``destination`` via a temporary in the same directory."""
    descriptor, temporary = _mkstemp(prefix=prefix, directory=destination.parent)
    temporary_pin = _pin_created_temporary(descriptor, temporary)
    temporary_descriptor: int | None = descriptor
    temporary_fingerprint: file_io.FileFingerprint | None = None
    try:
        temporary_fingerprint = temporary_pin.fingerprint
        owned_descriptor = temporary_descriptor
        temporary_descriptor = None
        if owned_descriptor is None:
            raise RuntimeError("temporary creation descriptor was already consumed")
        with _fdopen_owned(owned_descriptor, "wb") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        temporary_fingerprint = temporary_pin.fingerprint
        file_io.atomic_move_no_replace(
            _operation_path(temporary),
            _operation_path(destination),
            expected_identity=temporary_fingerprint[:2],
            expected_fingerprint=temporary_fingerprint,
            pinned_source=temporary_pin,
        )
        anchor = _anchor_for_directory(destination.parent)
        paths.fsync_directory(anchor.proc_path if anchor is not None else destination.parent)
    except file_io.PathChangedError as error:
        preserved = error.preserved_path
        raise ProviderError(
            "local-io",
            f"provider publication observed a changed entry"
            f"{f' preserved at {preserved}' if preserved is not None else ''}: {error}",
        ) from error
    except FileExistsError as error:
        raise ProviderError(
            "conflict", f"{destination} appeared before it could be published"
        ) from error
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not write {destination}: {error.strerror or error}"
        ) from error
    finally:
        try:
            if temporary_descriptor is not None:
                _close_descriptor_preserving_error(
                    temporary_descriptor,
                    f"the staging file {temporary}",
                )
        finally:
            _discard_pinned_temporary(
                temporary,
                temporary_pin,
                fallback=temporary_fingerprint,
            )
