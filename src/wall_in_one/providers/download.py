"""Where a download lands, and how it gets there without eating anything.

Two files prove a wallpaper is ours to delete, and `library.scan` demands
both: a **directory marker** saying we created the directory, and a **per-file
sidecar** saying we downloaded that particular file. A marker alone is not
enough, which is what keeps a file the user dropped into a managed directory
by hand out of reach of the delete button.

The names here are therefore not free -- they have to be the provider
provenance names `library.scan` already looks for, and they are the same names
the predecessor wrote, so an existing library keeps its ownership across the
rewrite. Pairing metadata is separate and never grants deletion authority.

Installation atomically renames staged temporaries without replacement.
Provenance is moved and synced first; media is the commit point, so process
death leaves either a complete pair or an ignored sidecar that age-bounded
recovery can remove. Each moved inode is verified after the rename, and a
published pathname is never unlinked for rollback. That ordering and
irreversibility matter because `finally` never runs after `SIGKILL` or power
loss.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import file_io, paths
from wall_in_one.providers.base import CancellationProbe, ProviderError, refuse_cancellation

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
    _require_real_directory(root, create=False)
    parent = root / MANAGED_PARENT
    _require_real_directory(parent, create=True)
    directory = parent / location.directory_name
    _require_real_directory(directory, create=True)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
    except OSError as error:
        raise ProviderError(
            "invalid-path", f"managed directory cannot be resolved: {error}"
        ) from error
    if (
        resolved_parent.parent != resolved_root
        or resolved_directory.parent != resolved_parent
        or not resolved_directory.is_relative_to(resolved_root)
    ):
        raise ProviderError("invalid-path", f"managed directory escapes {root}")
    # Validate the ownership marker before recovery is allowed to unlink even
    # an app-shaped staging name.  A foreign marker must make this directory
    # inert, not let cleanup run first and complain afterwards.
    marker = _write_marker(directory, location)
    recover_abandoned(directory, location)
    return directory, marker


def _require_real_directory(directory: Path, *, create: bool) -> None:
    """Require one path component to be a real directory, never a link.

    Provider destinations are write authority.  Following a symlink here
    would let ``<root>/Wall-in-One`` redirect downloads, markers and recovery
    unlinks outside the configured library root.
    """
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            raise ProviderError(
                "invalid-path", f"download root does not exist: {directory}"
            ) from None
        try:
            directory.mkdir()
        except FileExistsError:
            # A concurrent first download may have created the exact component
            # after our lstat. Reinspect it; only a real directory converges.
            pass
        except OSError as error:
            raise ProviderError(
                "local-io", f"could not create {directory}: {error.strerror or error}"
            ) from error
        try:
            info = directory.lstat()
        except OSError as error:
            raise ProviderError(
                "local-io",
                f"could not inspect created directory {directory}: {error.strerror or error}",
            ) from error
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not inspect {directory}: {error.strerror or error}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProviderError("invalid-path", f"managed path is not a real directory: {directory}")


def recover_abandoned(
    directory: Path,
    location: ManagedLocation,
    *,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Remove old, unmistakably app-owned remnants of interrupted installs.

    Recent files are left alone because another process may still be
    downloading. A final provider sidecar with no adjacent media is the only
    visible half-install possible under the sidecar-first commit protocol; it
    is inert to the scanner and safe to remove once old. Arbitrary dotfiles,
    symlinks and non-regular files are never touched.
    """
    marker = directory / location.marker_name
    try:
        raw_marker = file_io.read_regular_bytes(marker, MAX_SIDECAR_BYTES)
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
        with os.scandir(directory) as iterator:
            for entry in iterator:
                path = Path(entry.path)
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                    continue
                staging = _is_owned_staging(entry.name, info, prefixes)
                orphan_sidecar = _is_owned_orphan_sidecar(path, location, info.st_size)
                if not staging and not orphan_sidecar:
                    continue
                try:
                    claim = file_io.claim_for_deletion(
                        path,
                        expected_identity=(info.st_dev, info.st_ino),
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
                if orphan_sidecar:
                    media = Path(str(path)[: -len(location.sidecar_suffix)])
                    valid = valid and _is_owned_orphan_sidecar(
                        claim.path,
                        location,
                        claimed_info.st_size,
                        media_path=media,
                        logical_path=path,
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
                            f"provider recovery left an unverified entry preserved at {claim.path}",
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
                        f"could not safely clean provider staging; entry remains at {preserved}",
                    ) from error
                removed.append(path)
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not inspect provider staging: {error.strerror or error}"
        ) from error
    if removed:
        try:
            paths.fsync_directory(directory)
        except OSError as error:
            raise ProviderError(
                "local-io",
                f"cleaned abandoned provider staging but could not persist it: {error}",
            ) from error
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
    media = (
        Path(str(named_path)[: -len(location.sidecar_suffix)]) if media_path is None else media_path
    )
    if os.path.lexists(media):
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
        and document.get("path") == str(media)
    )


def _write_marker(directory: Path, location: ManagedLocation) -> Path:
    marker = directory / location.marker_name
    payload = encode_sidecar(location.marker_payload)
    try:
        existing = file_io.read_regular_bytes(marker, MAX_SIDECAR_BYTES)
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
            raced = file_io.read_regular_bytes(marker, MAX_SIDECAR_BYTES)
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

    Both moves are no-replace. The sidecar is published and synced first; the
    media move is the irreversible commit point. A hard kill or ordinary
    pre-commit failure can therefore leave an ignored orphan sidecar, never a
    visible media file that has lost the provenance needed to manage it safely.
    Once either final pathname has been moved this function will not unlink it
    for rollback: a concurrent local actor could have replaced the pathname,
    and check-then-unlink cannot prove inode ownership atomically. A failure
    after the media move is reported as a committed outcome with unknown final
    durability rather than pretending the download did not land.
    ``staged`` must already be in ``destination``'s directory -- it is, because
    the transport streams downloads into the directory they are destined for,
    which is also what makes the atomic rename same-filesystem by construction.
    """
    refuse_cancellation(cancelled)
    directory = destination.parent
    if staged.parent != directory:
        raise ProviderError("invalid-path", "staged download is not in its destination directory")
    try:
        staged_status = staged.lstat()
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not inspect staged download: {error.strerror or error}"
        ) from error
    if not stat.S_ISREG(staged_status.st_mode):
        raise ProviderError("invalid-path", "staged download is not a regular file")
    staged_identity = staged_status.st_dev, staged_status.st_ino
    sidecar_destination = Path(str(destination) + sidecar_suffix)

    descriptor, name = tempfile.mkstemp(prefix=SIDECAR_STAGING_PREFIX, dir=directory)
    sidecar_temporary = Path(name)
    sidecar_status = os.fstat(descriptor)
    sidecar_identity = sidecar_status.st_dev, sidecar_status.st_ino
    media_committed = False
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(sidecar_payload)
            sink.flush()
            os.fsync(sink.fileno())
        # Sidecar and media are both installed with no-replace moves. Observe
        # shutdown immediately before each boundary. Once moved, a final path
        # is deliberately irreversible: blindly unlinking it later could eat a
        # concurrent replacement at the same pathname.
        refuse_cancellation(cancelled)
        file_io.atomic_move_no_replace(
            sidecar_temporary,
            sidecar_destination,
            expected_identity=sidecar_identity,
        )
        paths.fsync_directory(directory)
        refuse_cancellation(cancelled)
        file_io.atomic_move_no_replace(
            staged,
            destination,
            expected_identity=staged_identity,
        )
        media_committed = True
        paths.fsync_directory(directory)
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
        _discard_owned(sidecar_temporary, sidecar_identity)
    _discard_owned(staged, staged_identity)
    return destination, sidecar_destination


def _discard_owned(path: Path, expected_identity: file_io.PathIdentity) -> None:
    """Best-effort removal of an unmistakably app-owned staging/sidecar path."""
    if file_io.discard_regular_if_same(path, expected_identity=expected_identity):
        try:
            paths.fsync_directory(path.parent)
        except OSError:
            # Recovery recognises the typed staging name or orphan sidecar later.
            return


def _atomic_write(destination: Path, payload: bytes, *, prefix: str) -> None:
    """Write ``payload`` to ``destination`` via a temporary in the same directory."""
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=destination.parent)
    temporary = Path(name)
    temporary_status = os.fstat(descriptor)
    temporary_identity = temporary_status.st_dev, temporary_status.st_ino
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        file_io.atomic_move_no_replace(
            temporary,
            destination,
            expected_identity=temporary_identity,
        )
        paths.fsync_directory(destination.parent)
    except FileExistsError as error:
        file_io.discard_regular_if_same(
            temporary,
            expected_identity=temporary_identity,
        )
        raise ProviderError(
            "conflict", f"{destination} appeared before it could be published"
        ) from error
    except OSError as error:
        file_io.discard_regular_if_same(
            temporary,
            expected_identity=temporary_identity,
        )
        raise ProviderError(
            "local-io", f"could not write {destination}: {error.strerror or error}"
        ) from error
