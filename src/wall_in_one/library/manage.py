"""Removing a wallpaper from the library.

The app could add files and never take one away: a wallpaper downloaded by
mistake had to be found in a file manager. That is the gap this closes, and it
is the one operation in the whole program that destroys something, so it is
also the one written most defensively.

Two verbs, because there are two kinds of file and they deserve different
treatment:

`remove` deletes something we downloaded, along with everything we wrote
beside it -- the sidecar that proved we owned it, a still we generated for it,
and that still's own sidecar. Ownership is re-established from disk at the
moment of deletion rather than trusted from the `MediaItem`, because the item
came from a scan that may be minutes old and a marker can be removed in
between. A stale record must never be what authorises an unlink.

`trash` moves a file the *user* put there into the freedesktop trash, where
they can get it back. `Ownership.USER` says "we never delete these", and that
remains true: moving a file to the trash at the user's explicit request is not
the same act as a program deciding on its own that a file is disposable.

Neither verb will touch a file outside the configured roots, and that is a
third case rather than a refinement of the first two. A Wallpaper Engine
wallpaper is in the library and is `Ownership.USER` -- the user did not put it
there, Steam did, and Steam will consider it missing. "Not ours to delete" and
"not ours to move either" are different claims, and only checking the first one
sent a 129 MB Workshop file to the trash on the machine this was written on.

Nothing here follows a symlink and nothing here deletes outside the roots it
was given, so a library entry pointing somewhere unexpected costs nothing.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

from wall_in_one import file_io, paths
from wall_in_one.library import pairing, scan, stills
from wall_in_one.library.model import Kind, MediaItem

#: Where the freedesktop home trash lives, relative to the data home.
TRASH_DIRECTORY: Final = "Trash"

#: How many `name (n)` variants to try before giving up on a unique name.
MAX_TRASH_ATTEMPTS: Final = 1000
SourceIdentity = tuple[int, int]


class ManageError(Exception):
    """A file could not be removed, with a machine-readable reason.

    Kinds in use: ``not-ours``, ``missing``, ``outside-root``, ``symlink``,
    ``cross-device``, ``local-io``.
    """

    def __init__(self, kind: str, message: str, *, committed: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        #: True only when the source removal/trash move crossed its commit
        #: point before a later durability check failed. Callers use this to
        #: retain the prewritten removal journal without mistaking an
        #: unavailable source drive for a completed operation.
        self.committed = committed

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


@dataclass(frozen=True, slots=True)
class Removal:
    """What actually went away."""

    item: MediaItem
    #: Every path unlinked, the wallpaper first.
    removed: tuple[Path, ...] = ()
    #: Paths we meant to remove and could not. Not fatal -- the wallpaper is
    #: gone, and a leftover sidecar is inert -- but worth being able to say.
    kept: tuple[Path, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        extra = len(self.removed) - 1
        beside = f" and {extra} file{'s' if extra != 1 else ''} beside it" if extra > 0 else ""
        return f"removed {self.item.path.name}{beside}"

    def cleanup_note(self) -> str:
        return _cleanup_note(self.kept)


@dataclass(frozen=True, slots=True)
class Trashed:
    """A committed trash move and the fate of app-owned companions."""

    item: MediaItem
    destination: Path
    removed_artifacts: tuple[Path, ...] = ()
    kept_artifacts: tuple[Path, ...] = ()

    def __str__(self) -> str:
        return str(self.destination)

    def cleanup_note(self) -> str:
        return _cleanup_note(self.kept_artifacts)


def _cleanup_note(kept: Sequence[Path]) -> str:
    if not kept:
        return ""
    names = ", ".join(path.name for path in kept[:3])
    omitted = len(kept) - 3
    more = f" and {omitted} more" if omitted > 0 else ""
    return f"; could not remove app-owned pairing files: {names}{more}"


def metadata_cleanup_note(failures: Sequence[str]) -> str:
    """Explain a metadata failure after the media operation already committed.

    Delete and trash cannot be rolled back safely.  The useful contract is
    therefore explicit partial failure plus a retry path, never an unqualified
    success which suggests the pairing/health records went away too.
    """
    if not failures:
        return ""
    shown = "; ".join(failures[:3])
    omitted = len(failures) - 3
    more = f"; and {omitted} more" if omitted > 0 else ""
    return (
        f"; metadata cleanup is incomplete: {shown}{more}. "
        "The wallpaper is already gone; fix the state-directory problem and "
        "refresh the library to retry"
    )


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    """Whether ``path`` is inside one of ``roots``, symlinks not followed.

    `Path.is_relative_to` is a string comparison, which is what is wanted here:
    resolving first would let a symlink inside a root vouch for a target
    outside it.
    """
    return any(path.is_relative_to(root) for root in roots)


def _is_managed_on_disk(path: Path) -> bool:
    """Re-derive ownership from the filesystem, ignoring what we were told.

    Both halves are required, exactly as `library.scan` requires them: a
    directory marker saying we made the directory, and a per-file sidecar
    saying we downloaded this particular file. Either alone would let a file
    the user dropped into a managed directory be deleted as though it were
    ours.
    """
    return scan.is_managed_directory(path.parent) and scan.has_download_sidecar(path)


def _companions(
    item: MediaItem,
    roots: tuple[Path, ...],
    legacy_selected_still: Path | None = None,
) -> list[Path]:
    """Everything we wrote beside ``item`` and should take with it.

    Only files whose names we generate: the download sidecar, the pairing
    sidecar, and a still that lives in the managed `Automatic Stills`
    directory. A still the user made themselves and named by convention is
    theirs and stays, even though it is about to have nothing to pair with.
    """
    found: list[Path] = []
    provenance = scan.download_provenance(item.path)
    provider_suffix = (
        {
            "MotionBGS": ".motionbgs.json",
            "Wallhaven": ".wallhaven.json",
        }.get(provenance)
        if provenance is not None
        else None
    )
    if provider_suffix is not None:
        candidate = item.path.with_name(item.path.name + provider_suffix)
        if candidate.is_file() and not candidate.is_symlink():
            found.append(candidate)

    for candidate in pairing_artifact_paths(
        item, roots, legacy_selected_still=legacy_selected_still
    ):
        if candidate.is_file() and not candidate.is_symlink():
            found.append(candidate)
    # A path can be both a provider and pairing sidecar only if a future
    # provider accidentally adopts our reserved suffix.  Keep deletion
    # idempotent anyway rather than reporting a harmless second unlink as a
    # failure.
    return list(dict.fromkeys(found))


def pairing_artifact_paths(
    item: MediaItem,
    roots: Sequence[Path] = (),
    *,
    legacy_selected_still: Path | None = None,
) -> tuple[Path, ...]:
    """Exact app-owned pairing paths derived from one media identity.

    Candidates are returned whether or not they currently exist.  Besides
    driving filesystem cleanup, that lets the app scrub stored references to
    an automatic still after the unlink has already committed.
    """
    bounded = tuple(roots)
    found: list[Path] = []
    pairing_sidecar = item.path.with_name(item.path.name + pairing.SIDECAR_SUFFIX)
    if _within(pairing_sidecar, bounded):
        found.append(pairing_sidecar)

    for root in bounded:
        generated = stills.automatic_destination(item, root)
        if generated is None:
            break
        found.append(generated)
        found.append(generated.with_name(generated.name + pairing.SIDECAR_SUFFIX))

    if legacy_selected_still is not None and _within(legacy_selected_still, bounded):
        expected = (
            f"video:{item.path}"
            if item.kind is Kind.VIDEO
            else item.scene
            if item.kind is Kind.SCENE
            else ""
        )
        if expected and pairing.legacy_automatic_identity(legacy_selected_still) == expected:
            found.append(legacy_selected_still)
            found.append(
                legacy_selected_still.with_name(legacy_selected_still.name + pairing.SIDECAR_SUFFIX)
            )
    return tuple(dict.fromkeys(found))


def discard_pairing_artifacts(
    item: MediaItem,
    roots: Sequence[Path] = (),
    *,
    legacy_selected_still: Path | None = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Best-effort removal of app-owned stills and pairing sidecars.

    This deliberately does not touch ``item.path``.  It is shared by explicit
    remove/trash and by the one trustworthy external-delete signal: a
    Workshop item vanished while its content root remained mounted.  The
    return is ``(removed, kept)`` so callers and tests can account for an
    unlink which failed without ever widening deletion authority.
    """
    bounded = tuple(roots)
    try:
        # This is the commit boundary shared with automatic-still
        # publication. A capture already publishing finishes first and is
        # then cleaned; a capture still rendering waits until this cleanup has
        # won, revalidates its now-absent/replaced source, and discards its
        # private temporary instead of resurrecting an orphan still.
        with stills.source_lifecycle_lock(item):
            return _discard_pairing_artifacts_locked(
                item,
                bounded,
                legacy_selected_still=legacy_selected_still,
            )
    except OSError:
        # Cleanup is best effort after the media operation has committed.
        # Never bypass a lifecycle lock which could not be trusted. Include
        # deterministic candidates even when they are not visible *yet*: the
        # publisher holding the lock may be between source validation and its
        # atomic replace. Reporting them as retained keeps the durable removal
        # journal alive so a later retry cleans anything that publisher lands.
        retained = list(
            pairing_artifact_paths(
                item,
                bounded,
                legacy_selected_still=legacy_selected_still,
            )
        )
        with contextlib.suppress(OSError):
            retained.extend(_companions(item, bounded, legacy_selected_still))
        return (), tuple(dict.fromkeys(retained))


def _discard_pairing_artifacts_locked(
    item: MediaItem,
    roots: tuple[Path, ...],
    *,
    legacy_selected_still: Path | None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Remove pairing artifacts while the source lifecycle lock is held."""
    removed: list[Path] = []
    kept: list[Path] = []
    for companion in _companions(item, roots, legacy_selected_still):
        try:
            expected = file_io.path_identity(companion)
        except FileNotFoundError:
            # Another cleanup already completed the same idempotent work.
            continue
        except OSError:
            kept.append(companion)
            continue
        try:
            if not file_io.discard_regular_if_same(
                companion,
                expected_identity=expected,
            ):
                if not os.path.lexists(companion):
                    continue
                raise OSError(f"could not safely discard {companion}")
            paths.fsync_directory(companion.parent)
        except OSError:
            kept.append(companion)
        else:
            removed.append(companion)
    return tuple(removed), tuple(kept)


def _require_source_identity(path: Path, expected: SourceIdentity | None) -> SourceIdentity:
    """Refuse an unjournaled operation or a path replaced since prepare."""
    if expected is None:
        raise ManageError(
            "unrecorded",
            "the removal has no persisted source identity; nothing was changed",
        )
    try:
        current = _identity(path)
    except OSError as error:
        raise ManageError(
            "missing", f"{path} is no longer the wallpaper prepared for removal"
        ) from error
    if current != expected:
        raise ManageError(
            "changed",
            f"{path} was replaced after removal was prepared; nothing was changed",
        )
    return current


def remove(
    item: MediaItem,
    roots: tuple[Path, ...] = (),
    *,
    expected_source: SourceIdentity | None = None,
    operation_token: str | None = None,
) -> Removal:
    """Delete a wallpaper we downloaded, and everything we wrote beside it.

    Refuses anything else. The refusal is the point of the function: the app
    writes into a directory the user also keeps their own wallpapers in, and
    the only thing standing between a delete button and somebody's photographs
    is this check.
    """
    path = item.path
    if operation_token is None:
        raise ManageError(
            "unrecorded",
            "the removal has no persisted operation token; nothing was changed",
        )
    if item.provider == scan.WORKSHOP_PROVIDER or item.kind is Kind.SCENE:
        raise ManageError("not-ours", f"{path.name} belongs to Steam, not this app")
    if not roots or not _within(path, roots):
        raise ManageError("outside-root", f"{path} is not inside the library")
    if path.is_symlink():
        raise ManageError("symlink", f"{path} is a symbolic link, so it is not ours to delete")
    if not path.is_file():
        raise ManageError("missing", f"{path} is no longer there")
    _require_source_identity(path, expected_source)
    if not _is_managed_on_disk(path):
        # Deliberately phrased as ours-or-not rather than as a permission
        # problem: the user has every right to delete this file, just not
        # through us.
        raise ManageError(
            "not-ours",
            f"{path.name} is your own file, not one this app downloaded",
        )
    # Provenance reads several files. Recheck the prepared inode immediately
    # before granting the destructive operation filesystem authority.
    source_identity = _require_source_identity(path, expected_source)

    removed: list[Path] = []
    kept: list[Path] = []
    # The wallpaper goes first. If the run stops after this, what is left
    # behind is inert metadata rather than a wallpaper with no sidecar, which
    # would read as an unmanaged file the next time anything looked.
    try:
        claim = file_io.claim_for_deletion(
            path,
            expected_identity=source_identity,
            operation_token=operation_token,
        )
    except file_io.PathChangedError as error:
        raise ManageError(
            "changed",
            f"{path} was replaced while removal claimed it; nothing was deleted",
        ) from error
    except FileNotFoundError as error:
        raise ManageError("missing", f"{path} is no longer there") from error
    except ValueError as error:
        raise ManageError("unrecorded", f"invalid persisted removal token: {error}") from error
    except OSError as error:
        raise ManageError(
            "local-io", f"could not claim {path} for removal: {error.strerror or error}"
        ) from error
    # Persist the replayable claim before destroying its contents. These
    # intermediate syncs are best effort: the final parent sync below is the
    # authoritative durability boundary for a completed deletion. If the
    # process dies in between, the deterministic journal-token name remains
    # available for replay.
    with contextlib.suppress(OSError):
        paths.fsync_directory(claim.path.parent)
    with contextlib.suppress(OSError):
        paths.fsync_directory(path.parent)
    try:
        claim.discard()
    except OSError as error:
        try:
            restored = claim.restore()
        except OSError:
            restored = False
        if restored:
            with contextlib.suppress(OSError):
                paths.fsync_directory(path.parent)
            raise ManageError(
                "local-io",
                f"could not remove {path}; the original file was restored: "
                f"{error.strerror or error}",
            ) from error
        raise ManageError(
            "local-io",
            f"could not finish removing {path}; its claimed entry was preserved at {claim.path}",
            committed=True,
        ) from error
    try:
        paths.fsync_directory(path.parent)
    except OSError as error:
        raise ManageError(
            "local-io",
            f"removed {path}, but could not persist its removal: {error}",
            committed=True,
        ) from error
    removed.append(path)

    discarded, retained = discard_pairing_artifacts(item, roots)
    removed.extend(discarded)
    kept.extend(retained)
    return Removal(item=item, removed=tuple(removed), kept=tuple(kept))


def recover_removal_claim(
    path: Path,
    *,
    expected_source: SourceIdentity | None,
    operation_token: str,
) -> bool:
    """Finish an exact media deletion claimed before a process died.

    This is called only while replaying the durable removal intent which owns
    ``operation_token``. ``False`` means there was no claimed entry (death was
    before the move or after its unlink); ``True`` means the exact journaled
    inode was found and deleted. Unexpected entries remain preserved.
    """
    if expected_source is None:
        raise ManageError(
            "unrecorded",
            "the removal claim has no persisted source identity; nothing was changed",
        )
    try:
        claim = file_io.recover_deletion_claim(
            path,
            expected_identity=expected_source,
            operation_token=operation_token,
        )
    except (OSError, ValueError) as error:
        raise ManageError(
            "local-io",
            f"could not recover the pending removal claim for {path}: {error}",
        ) from error
    if claim is None:
        return False
    try:
        claim.discard()
        paths.fsync_directory(path.parent)
    except OSError as error:
        raise ManageError(
            "local-io",
            f"could not finish the pending removal claim for {path}: {error}",
        ) from error
    return True


# -- the freedesktop trash -----------------------------------------------


def trash_directory() -> Path:
    """The home trash, per the freedesktop specification."""
    return paths.data_home() / TRASH_DIRECTORY


def _trash_names(original: Path) -> Sequence[str]:
    """Candidate names, keeping the extension recognisable.

    The suffix goes before the extension so that a restored `foo (1).mp4` is
    still obviously a video, which `foo.mp4 (1)` would not be. Availability is
    deliberately not checked here: only a no-replace link may claim a name
    without racing another trash operation.
    """
    return tuple(
        original.name if attempt == 0 else f"{original.stem} ({attempt}){original.suffix}"
        for attempt in range(MAX_TRASH_ATTEMPTS)
    )


def _identity(path: Path) -> tuple[int, int]:
    status = path.lstat()
    return status.st_dev, status.st_ino


def _unlink_if_same(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the directory entry this operation installed.

    The atomic claim is what makes "same" authoritative: a check followed by
    ``path.unlink()`` could otherwise delete a replacement installed between
    those two operations.
    """
    if not file_io.discard_regular_if_same(path, expected_identity=identity):
        return
    try:
        paths.fsync_directory(path.parent)
    except OSError:
        # Rollback is best effort. Leaving an inert record or private claim is
        # safer than masking the original failure or unlinking a replacement.
        return


def is_removable(item: MediaItem, roots: Sequence[Path] = ()) -> bool:
    """Whether this app may remove or move ``item`` at all.

    Inside a configured root, or nowhere. Being in the library is not enough:
    Steam's Workshop content is scanned into the library and is emphatically
    not the app's to move.
    """
    return (
        bool(roots)
        and (item.deletable or item.provider == "local")
        and item.provider != scan.WORKSHOP_PROVIDER
        and item.kind is not Kind.SCENE
        and _within(item.path, tuple(roots))
    )


def trash(
    item: MediaItem,
    roots: Sequence[Path] = (),
    *,
    expected_source: SourceIdentity | None = None,
) -> Trashed:
    """Move ``item`` into trash and report its destination and artifact cleanup.

    The reversible verb, and therefore the right one for a file the user made.
    Only the home trash is implemented: a wallpaper on another filesystem
    cannot be renamed into it, and the specification's per-device `.Trash-$uid`
    fallback is a second mechanism with its own failure modes. Saying so is
    better than silently unlinking something the user expected to be able to
    get back.
    """
    path = item.path
    if item.provider != "local" or not is_removable(item, roots):
        raise ManageError(
            "not-ours",
            f"{path.name} lives outside your wallpaper folders, so it is not this app's to move",
        )
    if path.is_symlink() or not path.is_file():
        raise ManageError("missing", f"{path} is no longer there")
    _require_source_identity(path, expected_source)

    files = trash_directory() / "files"
    info = trash_directory() / "info"
    try:
        paths.ensure_directory(files)
        paths.ensure_directory(info)
    except OSError as error:
        raise ManageError(
            "local-io", f"could not prepare the trash: {error.strerror or error}"
        ) from error

    original = path.absolute()
    original_identity = _identity(original)
    if original_identity != expected_source:
        raise ManageError(
            "changed",
            f"{original} was replaced after removal was prepared; nothing was changed",
        )
    stamp = datetime.now().replace(microsecond=0).isoformat()
    payload = f"[Trash Info]\nPath={quote(str(original), safe='/')}\nDeletionDate={stamp}\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".wall-in-one-trashinfo-", dir=info)
    temporary = Path(temporary_name)
    temporary_status = os.fstat(descriptor)
    temporary_identity = temporary_status.st_dev, temporary_status.st_ino
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
    except OSError as error:
        _unlink_if_same(temporary, temporary_identity)
        raise ManageError(
            "local-io", f"could not write the trash record: {error.strerror or error}"
        ) from error

    try:
        for name in _trash_names(original):
            record = info / f"{name}.trashinfo"
            try:
                os.link(temporary, record, follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError as error:
                raise ManageError(
                    "local-io",
                    f"could not reserve a trash record: {error.strerror or error}",
                ) from error
            record_identity = temporary_identity
            try:
                paths.fsync_directory(info)
            except OSError as error:
                _unlink_if_same(record, record_identity)
                raise ManageError(
                    "local-io",
                    f"could not persist the trash record: {error.strerror or error}",
                ) from error

            destination = files / name
            try:
                file_io.atomic_move_no_replace(
                    original,
                    destination,
                    expected_identity=original_identity,
                    require_regular=True,
                )
            except FileExistsError:
                _unlink_if_same(record, record_identity)
                continue
            except file_io.PathChangedError as error:
                # If restoration was blocked, the moved entry remains in the
                # trash and its already-durable record must stay with it.
                if error.preserved_path is not None:
                    raise ManageError(
                        "changed",
                        f"{original} changed while it was being moved; the unverified "
                        f"entry was preserved at {error.preserved_path}",
                        committed=True,
                    ) from error
                _unlink_if_same(record, record_identity)
                raise ManageError(
                    "changed",
                    f"{original} was replaced while it was being moved; nothing was deleted",
                ) from error
            except OSError as error:
                _unlink_if_same(record, record_identity)
                if error.errno == errno.EXDEV:
                    raise ManageError(
                        "cross-device",
                        f"{original.name} is on another filesystem, "
                        "so it cannot be moved to the trash",
                    ) from error
                if error.errno == errno.ENOENT:
                    raise ManageError("missing", f"{original} is no longer there") from error
                raise ManageError(
                    "local-io", f"could not move {original}: {error.strerror or error}"
                ) from error

            try:
                paths.fsync_directory(files)
                paths.fsync_directory(original.parent)
            except OSError as error:
                # The atomic move already removed the source. Rolling back the
                # trash copy here would be data loss, so keep it and report the
                # durability uncertainty explicitly.
                raise ManageError(
                    "local-io",
                    f"moved {original}, but could not persist the move: {error}",
                    committed=True,
                ) from error
            # The source move is the commit point.  From here, discard only
            # paths which are independently proven to have been generated for
            # this item.  A custom representative still is never among them.
            removed_artifacts, kept_artifacts = discard_pairing_artifacts(item, roots)
            return Trashed(item, destination, removed_artifacts, kept_artifacts)
        raise ManageError(
            "local-io", f"the trash already holds {MAX_TRASH_ATTEMPTS} files so named"
        )
    finally:
        _unlink_if_same(temporary, temporary_identity)
