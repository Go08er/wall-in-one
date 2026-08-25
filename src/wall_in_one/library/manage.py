"""Removing a wallpaper from the library.

The app could add files and never take one away: a wallpaper downloaded by
mistake had to be found in a file manager. That is the gap this closes, and it
is the one operation in the whole program that destroys something, so it is
also the one written most defensively.

Two verbs, because there are two kinds of file and they deserve different
treatment:

`remove` permanently removes the public generation we downloaded, along with
the exact app-owned companions pinned before commit -- the sidecar that proved
we owned it, a still we generated for it, and that still's own sidecar.
Ownership is re-established from disk at the moment of deletion rather than
trusted from the `MediaItem`, because the item came from a scan that may be
minutes old and a marker can be removed in between. A stale record must never
authorise physical removal.

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
import stat
import sys
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
SourceFingerprint = file_io.FileFingerprint
_PROVIDER_SIDECAR_PROVIDERS: Final = {
    ".motionbgs.json": "MotionBGS",
    ".wallhaven.json": "Wallhaven",
}


class ManageError(Exception):
    """A file could not be removed, with a machine-readable reason.

    Kinds in use: ``not-ours``, ``missing``, ``outside-root``, ``symlink``,
    ``cross-device``, ``local-io``.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        committed: bool = False,
        retain_intent: bool = False,
        physical: Removal | Trashed | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        #: True only when the source removal/trash move crossed its commit
        #: point before a later durability check failed. Callers use this to
        #: retain the prewritten removal journal without mistaking an
        #: unavailable source drive for a completed operation.
        self.committed = committed
        #: True when no media deletion committed, but an unverified entry was
        #: moved into the durable token claim and could not be restored. The
        #: prepared journal must remain uncommitted so replay preserves and
        #: reports that entry instead of orphaning its only authority record.
        self.retain_intent = retain_intent
        #: Exact physical outcome assembled from the capabilities retained
        #: before commit. This is present when a post-commit durability error
        #: occurs after companion cleanup has still been completed/reported.
        self.physical = physical

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def _cleanup_error(
    error: OSError,
    *,
    path: Path,
    committed: bool,
    physical: Removal | Trashed | None = None,
) -> ManageError:
    """Preserve a destructive outcome hidden by context-manager teardown."""
    context: BaseException | None = error.__context__
    visited: set[int] = set()
    while context is not None and id(context) not in visited:
        visited.add(id(context))
        if isinstance(context, ManageError):
            detail = str(context.args[0]) if context.args else str(context)
            return ManageError(
                context.kind,
                f"{detail}; additionally, cleanup for {path} failed: {error}",
                committed=context.committed or committed,
                retain_intent=context.retain_intent,
                physical=context.physical if context.physical is not None else physical,
            )
        context = context.__context__
    return ManageError(
        "local-io",
        f"could not secure pairing cleanup for {path}: {error}",
        committed=committed,
        physical=physical,
    )


def _release_source_pins(
    source_pin: file_io.PinnedPath,
    prepared_pin: file_io.PinnedPath | None,
) -> None:
    """Release both borrowed capabilities without changing an operation result."""
    active_error = sys.exception()
    for description, pin in (
        ("retained source", source_pin),
        ("prepared source", prepared_pin),
    ):
        if pin is None:
            continue
        try:
            pin.close()
        except OSError as close_error:
            if active_error is not None:
                active_error.add_note(f"also could not close the {description} pin: {close_error}")


@dataclass(frozen=True, slots=True)
class Removal:
    """What actually went away."""

    item: MediaItem
    #: Every path removed from its public namespace, the wallpaper first.
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


@dataclass(frozen=True, slots=True)
class _PinnedArtifact:
    """One pre-commit companion generation retained through media mutation."""

    logical: Path
    access: Path
    pin: file_io.PinnedPath
    identity: SourceIdentity
    fingerprint: SourceFingerprint
    deletion_authority: scan.DownloadAuthority | None = None


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


def _is_managed_on_disk(path: Path, *, logical_path: Path | None = None) -> bool:
    """Re-derive ownership from the filesystem, ignoring what we were told.

    Both halves are required, exactly as `library.scan` requires them: a
    directory marker saying we made the directory, and a per-file sidecar
    saying we downloaded this particular file. Either alone would let a file
    the user dropped into a managed directory be deleted as though it were
    ours.
    """
    return scan.is_managed_directory(path.parent) and scan.has_download_sidecar(
        path,
        expected_path=logical_path,
    )


def _artifact_access_path(
    candidate: Path,
    *,
    source_root: Path | None,
    lookup_root: Path | None,
    source_parent: Path | None,
    lookup_parent: Path | None,
) -> Path | None:
    """Map one logical artifact into a retained source-root capability."""
    if source_root is None and lookup_root is None and lookup_parent is None:
        return candidate
    if source_root is None or lookup_root is None or source_parent is None or lookup_parent is None:
        raise ValueError("artifact cleanup requires logical and retained root/parent pairs")
    if candidate.parent == source_parent:
        return lookup_parent / candidate.name
    # Generated stills can live under roots[0], and even a same-root path would
    # require following unpinned descendant directories. No identity for that
    # artifact parent is journaled, so leave the inert app-owned file rather
    # than traverse a directory which could have become a symlink.
    return None


def _retained_artifact_access_path(
    candidate: Path,
    *,
    source_root: Path | None,
    lookup_root: Path | None,
    source_parent: Path,
    lookup_parent: Path | None,
    source_context: file_io.PinnedDirectoryContext | None,
    access_stack: contextlib.ExitStack,
    parent_accesses: dict[Path, Path | None],
) -> Path | None:
    """Address a candidate through a retained exact parent capability."""
    direct = _artifact_access_path(
        candidate,
        source_root=source_root,
        lookup_root=lookup_root,
        source_parent=source_parent,
        lookup_parent=lookup_parent,
    )
    if direct is not None:
        # A bare public path is not a capability. Callers without the retained
        # source context fall through to an independently pinned root/parent.
        if source_context is None:
            return None
        return direct
    if source_context is None or source_root is None:
        return None
    if source_context.root != source_root:
        raise ValueError("artifact cleanup context does not match its logical source root")
    try:
        candidate.relative_to(source_root)
    except ValueError:
        return None
    logical_parent = candidate.parent.absolute()
    if logical_parent not in parent_accesses:
        try:
            access = access_stack.enter_context(source_context.access_descendant(candidate))
        except FileNotFoundError:
            # While holding the source lifecycle lock, a missing parent proves
            # this deterministic artifact is absent. Cache that observation so
            # sibling candidates cannot be reached through a later directory
            # generation in the same transaction.
            parent_accesses[logical_parent] = None
            return None
        parent_accesses[logical_parent] = access.parent
    access_parent = parent_accesses[logical_parent]
    if access_parent is None:
        return None
    try:
        with source_context.access_descendant(candidate) as current:
            retained = access_parent.stat()
            observed = current.parent.stat()
    except FileNotFoundError as error:
        raise file_io.PathChangedError(
            f"artifact parent {candidate.parent} changed during retained traversal"
        ) from error
    if (
        not stat.S_ISDIR(retained.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or (retained.st_dev, retained.st_ino) != (observed.st_dev, observed.st_ino)
    ):
        raise file_io.PathChangedError(f"artifact parent {candidate.parent} changed generation")
    return access_parent / candidate.name


def _independently_pinned_artifact_access(
    candidate: Path,
    roots: tuple[Path, ...],
    *,
    access_stack: contextlib.ExitStack,
    parent_contexts: dict[Path, file_io.PinnedDirectoryContext | None],
    root_pins: dict[Path, file_io.PinnedPath | None],
) -> Path:
    """Address an artifact beneath a configured root other than the source root."""
    try:
        root = next(root for root in roots if candidate.is_relative_to(root))
    except StopIteration as error:
        raise ValueError(f"artifact {candidate} is outside configured roots") from error
    absolute_root = root.absolute()
    absolute_parent = candidate.parent.absolute()
    if absolute_parent in parent_contexts:
        existing = parent_contexts[absolute_parent]
        if existing is None:
            raise FileNotFoundError(absolute_parent)
        existing.verify_public()
        return existing.child(candidate.name)

    if absolute_root in root_pins:
        root_pin = root_pins[absolute_root]
        if root_pin is None:
            raise FileNotFoundError(absolute_root)
    else:
        try:
            root_status = absolute_root.lstat()
        except FileNotFoundError:
            root_pins[absolute_root] = None
            raise
        if not stat.S_ISDIR(root_status.st_mode):
            raise file_io.PathChangedError(
                f"artifact root {absolute_root} is not a retained directory"
            )
        root_pin = file_io.pin_directory_path(
            absolute_root,
            expected_identity=(root_status.st_dev, root_status.st_ino),
        )
        access_stack.callback(root_pin.close)
        root_pins[absolute_root] = root_pin
    if absolute_root in root_pins and root_pin is not None:
        verification = file_io.pin_directory_path(
            absolute_root,
            expected_identity=root_pin.identity,
        )
        verification.close()
    try:
        parent_status = absolute_parent.lstat()
    except FileNotFoundError:
        parent_contexts[absolute_parent] = None
        raise
    if not stat.S_ISDIR(parent_status.st_mode):
        raise file_io.PathChangedError(
            f"artifact parent {candidate.parent} is not a retained directory"
        )
    context = file_io.pin_directory_beneath(
        absolute_root,
        absolute_parent,
        expected_root_identity=root_pin.identity,
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    access_stack.callback(context.close)
    parent_contexts[absolute_parent] = context
    context.verify_public()
    return context.child(candidate.name)


def _companions(
    item: MediaItem,
    roots: tuple[Path, ...],
    legacy_selected_still: Path | None = None,
    *,
    source_root: Path | None = None,
    lookup_root: Path | None = None,
    lookup_parent: Path | None = None,
    source_context: file_io.PinnedDirectoryContext | None = None,
    access_stack: contextlib.ExitStack,
) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """Everything we wrote beside ``item`` and should take with it.

    Only files whose names we generate: the download sidecar, the pairing
    sidecar, and a still that lives in the managed `Automatic Stills`
    directory. A still the user made themselves and named by convention is
    theirs and stays, even though it is about to have nothing to pair with.
    """
    found: list[tuple[Path, Path]] = []
    kept: list[Path] = []
    source_parent_accesses: dict[Path, Path | None] = {}
    independent_parent_contexts: dict[Path, file_io.PinnedDirectoryContext | None] = {}
    independent_root_pins: dict[Path, file_io.PinnedPath | None] = {}

    def retain_candidate(candidate: Path) -> None:
        try:
            access = _retained_artifact_access_path(
                candidate,
                source_root=source_root,
                lookup_root=lookup_root,
                source_parent=item.path.parent,
                lookup_parent=lookup_parent,
                source_context=source_context,
                access_stack=access_stack,
                parent_accesses=source_parent_accesses,
            )
            if access is None and (
                source_context is None
                or source_root is None
                or not candidate.is_relative_to(source_root)
            ):
                if not _within(candidate, roots):
                    return
                try:
                    access = _independently_pinned_artifact_access(
                        candidate,
                        roots,
                        access_stack=access_stack,
                        parent_contexts=independent_parent_contexts,
                        root_pins=independent_root_pins,
                    )
                except FileNotFoundError:
                    # A missing deterministic parent in the media's own root
                    # proves absence under the lifecycle lock. A different
                    # configured root may instead be temporarily unavailable,
                    # so preserve that cross-root candidate for the report.
                    same_source_root = any(
                        item.path.is_relative_to(root) and candidate.is_relative_to(root)
                        for root in roots
                    )
                    if not same_source_root:
                        kept.append(candidate)
                    return
        except FileNotFoundError:
            return
        except OSError, ValueError:
            kept.append(candidate)
            return
        if access is None:
            return
        try:
            status = access.lstat()
        except FileNotFoundError:
            return
        except OSError:
            kept.append(candidate)
            return
        if stat.S_ISREG(status.st_mode):
            found.append((candidate, access))
        else:
            kept.append(candidate)

    # Enumerate both authority-bearing suffixes. ``download_provenance``
    # intentionally returns the first valid provider for scanning, but removal
    # must withdraw every valid authority before making the media name vacant.
    for provider_suffix in _PROVIDER_SIDECAR_PROVIDERS:
        candidate = item.path.with_name(item.path.name + provider_suffix)
        retain_candidate(candidate)

    for candidate in pairing_artifact_paths(
        item, roots, legacy_selected_still=legacy_selected_still
    ):
        retain_candidate(candidate)
    # A path can be both a provider and pairing sidecar only if a future
    # provider accidentally adopts our reserved suffix.  Keep deletion
    # idempotent anyway rather than reporting a harmless second unlink as a
    # failure.
    return list(dict.fromkeys(found)), list(dict.fromkeys(kept))


def _logical_preserved_artifact(
    companion: Path,
    preserved: Path | None,
    *,
    access_parent: Path,
) -> Path:
    """Translate a private access-path quarantine back to its durable name."""
    if preserved is None:
        return companion
    try:
        relative = preserved.relative_to(access_parent)
    except ValueError:
        return companion
    return companion.parent / relative


def pairing_artifact_paths(
    item: MediaItem,
    roots: Sequence[Path] = (),
    *,
    legacy_selected_still: Path | None = None,
) -> tuple[Path, ...]:
    """Exact app-owned pairing paths derived from one media identity.

    Candidates are returned whether or not they currently exist. Besides
    driving filesystem cleanup, that lets the app scrub stored references to
    an automatic still after public media removal has committed.
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
    source_root: Path | None = None,
    lookup_root: Path | None = None,
    lookup_parent: Path | None = None,
    source_context: file_io.PinnedDirectoryContext | None = None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Best-effort removal of app-owned stills and pairing sidecars.

    This deliberately does not touch ``item.path`` and is used only by a live
    explicit remove/trash transaction carrying pre-commit filesystem
    capabilities. Observed Workshop uninstalls clear authored metadata only;
    they never rediscover deterministic physical companions. The return is
    ``(removed, kept)`` so callers and tests can account for cleanup which
    failed without ever widening deletion authority.
    """
    bounded = tuple(roots)
    try:
        # This is the commit boundary shared with automatic-still
        # publication. A capture already publishing finishes first and is
        # then cleaned; a capture still rendering waits until this cleanup has
        # won, revalidates its now-absent/replaced source, and discards its
        # private temporary instead of resurrecting an orphan still.
        with (
            stills.media_path_lifecycle_lock(item.path),
            stills.source_lifecycle_lock(item),
            contextlib.ExitStack() as access_stack,
        ):
            return _discard_pairing_artifacts_locked(
                item,
                bounded,
                legacy_selected_still=legacy_selected_still,
                source_root=source_root,
                lookup_root=lookup_root,
                lookup_parent=lookup_parent,
                source_context=source_context,
                access_stack=access_stack,
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
        with contextlib.ExitStack() as fallback_stack, contextlib.suppress(OSError):
            visible, inaccessible = _companions(
                item,
                bounded,
                legacy_selected_still,
                source_root=source_root,
                lookup_root=lookup_root,
                lookup_parent=lookup_parent,
                source_context=source_context,
                access_stack=fallback_stack,
            )
            retained.extend(logical for logical, _access in visible)
            retained.extend(inaccessible)
        return (), tuple(dict.fromkeys(retained))


def _discard_pairing_artifacts_locked(
    item: MediaItem,
    roots: tuple[Path, ...],
    *,
    legacy_selected_still: Path | None,
    source_root: Path | None,
    lookup_root: Path | None,
    lookup_parent: Path | None,
    source_context: file_io.PinnedDirectoryContext | None,
    access_stack: contextlib.ExitStack,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Remove pairing artifacts while the source lifecycle lock is held."""
    pinned, kept = _pin_pairing_artifacts_locked(
        item,
        roots,
        legacy_selected_still=legacy_selected_still,
        source_root=source_root,
        lookup_root=lookup_root,
        lookup_parent=lookup_parent,
        source_context=source_context,
        access_stack=access_stack,
    )
    return _discard_pinned_artifacts(pinned, kept=kept)


def _pin_pairing_artifacts_locked(
    item: MediaItem,
    roots: tuple[Path, ...],
    *,
    legacy_selected_still: Path | None,
    source_root: Path | None,
    lookup_root: Path | None,
    lookup_parent: Path | None,
    source_context: file_io.PinnedDirectoryContext | None,
    access_stack: contextlib.ExitStack,
) -> tuple[tuple[_PinnedArtifact, ...], tuple[Path, ...]]:
    """Retain every visible companion generation before the media commit."""
    try:
        companions, inaccessible = _companions(
            item,
            roots,
            legacy_selected_still,
            source_root=source_root,
            lookup_root=lookup_root,
            lookup_parent=lookup_parent,
            source_context=source_context,
            access_stack=access_stack,
        )
    except OSError:
        # A changed descendant directory makes every path reached through it
        # untrustworthy. Leave deterministic artifacts inert and report them;
        # media removal itself remains safe through its independent pin.
        return (), pairing_artifact_paths(
            item,
            roots,
            legacy_selected_still=legacy_selected_still,
        )

    pinned: list[_PinnedArtifact] = []
    kept: list[Path] = list(inaccessible)
    for companion, access in companions:
        try:
            pin = file_io.pin_regular_path(access)
        except FileNotFoundError:
            # It disappeared before the commit, so a later same-name entry is
            # a different lifecycle and must not be discovered after commit.
            continue
        except file_io.PathChangedError as error:
            kept.append(
                _logical_preserved_artifact(
                    companion,
                    error.preserved_path,
                    access_parent=access.parent,
                )
            )
            continue
        except OSError:
            kept.append(companion)
            continue
        access_stack.callback(pin.close)
        pinned_identity = pin.identity
        pinned_fingerprint = pin.fingerprint
        provider = next(
            (
                expected
                for suffix, expected in _PROVIDER_SIDECAR_PROVIDERS.items()
                if companion == item.path.with_name(item.path.name + suffix)
            ),
            None,
        )
        deletion_authority: scan.DownloadAuthority | None = None
        if provider is not None:
            try:
                raw = file_io.read_pinned_regular_bytes(
                    pin,
                    pairing.MAX_SIDECAR_BYTES,
                    expected_fingerprint=pinned_fingerprint,
                )
                deletion_authority = scan.download_authority_from_bytes(
                    raw,
                    expected_path=item.path,
                    expected_provider=provider,
                )
            except OSError, ValueError:
                deletion_authority = None
        pinned.append(
            _PinnedArtifact(
                companion,
                access,
                pin,
                pinned_identity,
                pinned_fingerprint,
                deletion_authority,
            )
        )
    return tuple(pinned), tuple(kept)


def _withdraw_download_authority(
    item: MediaItem,
    pinned: Sequence[_PinnedArtifact],
    *,
    required: bool,
    source_pin: file_io.PinnedPath,
    expected_fingerprint: SourceFingerprint,
) -> tuple[tuple[Path, ...], tuple[_PinnedArtifact, ...]]:
    """Durably remove provenance before its media path can become vacant."""
    provider_sidecars = tuple(
        artifact
        for artifact in pinned
        if any(
            artifact.logical == item.path.with_name(item.path.name + suffix)
            for suffix in _PROVIDER_SIDECAR_PROVIDERS
        )
    )
    authority_candidates = tuple(
        artifact for artifact in provider_sidecars if artifact.deletion_authority is not None
    )
    authority: tuple[_PinnedArtifact, ...] = ()
    if authority_candidates:
        try:
            media_size, media_digest = file_io.hash_pinned_regular(
                source_pin,
                expected_fingerprint=expected_fingerprint,
            )
        except OSError as error:
            raise ManageError(
                "changed",
                f"could not verify the exact contents of {item.path.name}; "
                "the wallpaper was left untouched",
            ) from error
        authority = tuple(
            artifact
            for artifact in authority_candidates
            if (
                artifact.deletion_authority is not None
                and artifact.deletion_authority.matches_generation(expected_fingerprint)
                and (artifact.deletion_authority.size, artifact.deletion_authority.sha256)
                == (media_size, media_digest)
            )
        )
        if required and not authority:
            raise ManageError(
                "changed",
                f"{item.path.name} no longer matches its exact download provenance; "
                "the wallpaper was left untouched",
            )
    if not authority:
        if not required:
            # Provider-shaped documents for another media generation are not
            # ours to unlink. Their generation binding leaves them inert after
            # this local file is moved to Trash.
            return (), tuple(
                candidate for candidate in pinned if candidate not in provider_sidecars
            )
        raise ManageError(
            "not-ours",
            f"{item.path.name} no longer has one exact download provenance record",
        )
    removed: list[Path] = []
    for artifact in authority:
        try:
            discarded = file_io.discard_regular_if_same(
                artifact.access,
                expected_identity=artifact.identity,
                expected_fingerprint=artifact.fingerprint,
                pinned_source=artifact.pin,
            )
            if not discarded:
                raise file_io.PathChangedError(
                    f"{artifact.logical} changed before download authority was withdrawn"
                )
            paths.fsync_directory(artifact.access.parent)
        except file_io.PathChangedError as error:
            detail = (
                f"; an unverified entry was preserved at {error.preserved_path}"
                if error.preserved_path is not None
                else ""
            )
            raise ManageError(
                "changed",
                f"{artifact.logical} changed before removal; "
                f"the wallpaper was left untouched{detail}",
            ) from error
        except OSError as error:
            raise ManageError(
                "local-io",
                f"could not withdraw download authority for {item.path}: {error}",
            ) from error
        removed.append(artifact.logical)
    # Invalid provider-shaped documents were never deletion authority and are
    # not app-owned companions. Exclude them without unlinking them.
    remaining = tuple(candidate for candidate in pinned if candidate not in provider_sidecars)
    return tuple(removed), remaining


def _discard_pinned_artifacts(
    pinned: Sequence[_PinnedArtifact],
    *,
    kept: Sequence[Path] = (),
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Discard only companion generations retained before the media commit."""
    removed: list[Path] = []
    retained = list(kept)
    for artifact in pinned:
        companion = artifact.logical
        access = artifact.access
        pin = artifact.pin
        try:
            if not file_io.discard_regular_if_same(
                access,
                expected_identity=artifact.identity,
                expected_fingerprint=artifact.fingerprint,
                pinned_source=pin,
            ):
                if not os.path.lexists(access):
                    continue
                raise OSError(f"could not safely discard {access}")
            paths.fsync_directory(access.parent)
        except file_io.PathChangedError as error:
            retained.append(
                _logical_preserved_artifact(
                    companion,
                    error.preserved_path,
                    access_parent=access.parent,
                )
            )
        except OSError:
            retained.append(companion)
        else:
            removed.append(companion)
    return tuple(removed), tuple(retained)


def _completed_removal_outcome(
    item: MediaItem,
    authority_removed: Sequence[Path],
    remaining_artifacts: Sequence[_PinnedArtifact],
    artifact_kept: Sequence[Path],
) -> Removal:
    """Assemble one committed delete after consuming every pre-pinned companion."""
    discarded, retained = _discard_pinned_artifacts(
        remaining_artifacts,
        kept=artifact_kept,
    )
    return Removal(
        item=item,
        removed=(item.path, *authority_removed, *discarded),
        kept=retained,
    )


def _completed_trash_outcome(
    item: MediaItem,
    destination: Path,
    authority_removed: Sequence[Path],
    remaining_artifacts: Sequence[_PinnedArtifact],
    artifact_kept: Sequence[Path],
) -> Trashed:
    """Assemble one committed trash move after consuming pre-pinned companions."""
    discarded, retained = _discard_pinned_artifacts(
        remaining_artifacts,
        kept=artifact_kept,
    )
    return Trashed(
        item,
        destination,
        tuple((*authority_removed, *discarded)),
        retained,
    )


def _require_exact_trash_record(
    record: Path,
    record_pin: file_io.PinnedPath,
    payload: bytes,
) -> None:
    """Require the public Trash record to expose our exact, intact payload."""
    fingerprint = record_pin.fingerprint
    encoded = file_io.read_pinned_regular_bytes(
        record_pin,
        len(payload),
        expected_fingerprint=fingerprint,
    )
    if encoded != payload:
        raise file_io.PathChangedError(f"trash record {record} no longer has its staged payload")
    verification = file_io.pin_regular_path(
        record,
        expected_identity=record_pin.identity,
        expected_fingerprint=fingerprint,
    )
    verification.close()


def _require_source_pin(
    path: Path,
    expected_identity: SourceIdentity | None,
    expected_fingerprint: SourceFingerprint | None,
    prepared_pin: file_io.PinnedPath | None,
    *,
    lookup_path: Path | None = None,
) -> file_io.PinnedPath:
    """Bind persisted removal authority to one still-live file generation."""
    if expected_identity is None or expected_fingerprint is None:
        raise ManageError(
            "unrecorded",
            "the removal has no persisted source generation; nothing was changed",
        )
    owns_pin = prepared_pin is None
    pin: file_io.PinnedPath | None = None
    try:
        pin = (
            file_io.pin_regular_path(
                path,
                expected_identity=expected_identity,
                expected_fingerprint=expected_fingerprint,
            )
            if prepared_pin is None
            else prepared_pin
        )
        if (
            pin.path != path
            or pin.identity != expected_identity
            or pin.fingerprint != expected_fingerprint
        ):
            raise file_io.PathChangedError(f"{path} changed after removal was prepared")
        if lookup_path is not None:
            access_pin = file_io.pin_regular_path(
                lookup_path,
                expected_identity=expected_identity,
                expected_fingerprint=expected_fingerprint,
            )
            if owns_pin:
                pin.close()
            pin = access_pin
            owns_pin = True
    except file_io.PathChangedError as error:
        if owns_pin and pin is not None:
            pin.close()
        raise ManageError(
            "changed", f"{path} was replaced after removal was prepared; nothing was changed"
        ) from error
    except (OSError, ValueError) as error:
        if owns_pin and pin is not None:
            pin.close()
        raise ManageError(
            "missing", f"{path} is no longer the wallpaper prepared for removal"
        ) from error
    return pin


def _validate_source_context(
    path: Path,
    *,
    prepared_pin: file_io.PinnedPath | None,
    lookup_path: Path | None,
    source_root: Path | None,
    lookup_root: Path | None,
    lookup_parent: Path | None,
    source_context: file_io.PinnedDirectoryContext | None,
) -> None:
    """Require one complete, internally consistent retained-path capability."""
    supplied = (lookup_path, source_root, lookup_root, lookup_parent, source_context)
    if all(value is None for value in supplied):
        if prepared_pin is not None:
            raise ManageError(
                "invalid-state",
                "a live prepared source reference requires its retained directory context",
            )
        return
    if any(value is None for value in supplied):
        raise ManageError(
            "invalid-state",
            "retained removal access requires its source path, root, parent, and context",
        )
    assert lookup_path is not None
    assert source_root is not None
    assert lookup_root is not None
    assert lookup_parent is not None
    assert source_context is not None
    try:
        matches = (
            source_context.root == source_root
            and source_context.directory == path.parent
            and source_context.root_anchor == lookup_root
            and source_context.directory_anchor == lookup_parent
            and source_context.child(path.name) == lookup_path
        )
    except OSError as error:
        raise ManageError(
            "invalid-state",
            f"the retained source directory for {path} is closed",
        ) from error
    if not matches:
        raise ManageError(
            "invalid-state",
            f"the retained source directory does not identify {path}",
        )


def remove(
    item: MediaItem,
    roots: tuple[Path, ...] = (),
    *,
    expected_source: SourceIdentity | None = None,
    expected_fingerprint: SourceFingerprint | None = None,
    prepared_pin: file_io.PinnedPath | None = None,
    operation_token: str | None = None,
    lookup_path: Path | None = None,
    source_root: Path | None = None,
    lookup_root: Path | None = None,
    lookup_parent: Path | None = None,
    source_context: file_io.PinnedDirectoryContext | None = None,
) -> Removal:
    """Delete a wallpaper we downloaded, and everything we wrote beside it.

    Refuses anything else. The refusal is the point of the function: the app
    writes into a directory the user also keeps their own wallpapers in, and
    the only thing standing between a delete button and somebody's photographs
    is this check.
    """
    path = item.path
    target = path if lookup_path is None else lookup_path
    try:
        _validate_source_context(
            path,
            prepared_pin=prepared_pin,
            lookup_path=lookup_path,
            source_root=source_root,
            lookup_root=lookup_root,
            lookup_parent=lookup_parent,
            source_context=source_context,
        )
    except ManageError:
        if prepared_pin is not None:
            prepared_pin.close()
        raise
    if operation_token is None:
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError(
            "unrecorded",
            "the removal has no persisted operation token; nothing was changed",
        )
    if item.provider == scan.WORKSHOP_PROVIDER or item.kind is Kind.SCENE:
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError("not-ours", f"{path.name} belongs to Steam, not this app")
    if not roots or not _within(path, roots):
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError("outside-root", f"{path} is not inside the library")
    if target.is_symlink():
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError("symlink", f"{path} is a symbolic link, so it is not ours to delete")
    if not target.is_file():
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError("missing", f"{path} is no longer there")
    try:
        source_pin = _require_source_pin(
            path,
            expected_source,
            expected_fingerprint,
            prepared_pin,
            lookup_path=lookup_path,
        )
    except ManageError:
        if prepared_pin is not None:
            prepared_pin.close()
        raise
    try:
        if expected_fingerprint is None:  # narrowed by _require_source_pin
            raise AssertionError("source fingerprint validation did not narrow")
        committed = False
        result: Removal | None = None
        try:
            with (
                stills.media_path_lifecycle_lock(path),
                stills.source_lifecycle_lock(item),
                contextlib.ExitStack() as access_stack,
            ):
                if not _is_managed_on_disk(target, logical_path=path):
                    # Deliberately phrased as ours-or-not rather than as a
                    # permission problem: the user has every right to delete
                    # this file, just not through us.
                    raise ManageError(
                        "not-ours",
                        f"{path.name} is your own file, not one this app downloaded",
                    )
                artifact_pins, artifact_kept = _pin_pairing_artifacts_locked(
                    item,
                    roots,
                    legacy_selected_still=None,
                    source_root=source_root,
                    lookup_root=lookup_root,
                    lookup_parent=lookup_parent,
                    source_context=source_context,
                    access_stack=access_stack,
                )
                result = _remove_pinned(
                    item,
                    operation_token=operation_token,
                    source_pin=source_pin,
                    expected_fingerprint=expected_fingerprint,
                    source_path=target,
                    artifact_pins=artifact_pins,
                    artifact_kept=artifact_kept,
                )
                committed = True
            if result is None:  # pragma: no cover - successful helper always returns
                raise AssertionError("committed removal produced no physical outcome")
            return result
        except OSError as error:
            raise _cleanup_error(
                error,
                path=path,
                committed=committed,
                physical=result,
            ) from error
    finally:
        _release_source_pins(source_pin, prepared_pin)


def _remove_pinned(
    item: MediaItem,
    *,
    operation_token: str,
    source_pin: file_io.PinnedPath,
    expected_fingerprint: SourceFingerprint,
    source_path: Path,
    artifact_pins: Sequence[_PinnedArtifact],
    artifact_kept: Sequence[Path],
) -> Removal:
    """Commit one managed removal while its prepare-time inode stays pinned."""
    path = item.path
    target = source_path
    source_identity = source_pin.identity
    if source_pin.fingerprint != expected_fingerprint:
        raise ManageError(
            "changed",
            f"{path} changed after removal was prepared; nothing was changed",
        )

    # Download provenance is deletion authority for this pathname. Withdraw
    # its exact pre-commit generation first so a crash after the media unlink
    # can never confer ownership on a later unrelated same-name file. A crash
    # in the narrow opposite window leaves the original media conservatively
    # unmanaged, not destructively authorized.
    authority_removed, remaining_artifacts = _withdraw_download_authority(
        item,
        artifact_pins,
        required=True,
        source_pin=source_pin,
        expected_fingerprint=expected_fingerprint,
    )
    try:
        claim = file_io.claim_for_deletion(
            target,
            expected_identity=source_identity,
            operation_token=operation_token,
            expected_fingerprint=expected_fingerprint,
            pinned_source=source_pin,
            logical_path=path,
        )
    except file_io.PathChangedError as error:
        if error.preserved_path is not None:
            with contextlib.suppress(OSError):
                paths.fsync_directory(error.preserved_path.parent)
            with contextlib.suppress(OSError):
                paths.fsync_directory(path.parent)
            raise ManageError(
                "changed",
                f"{path} changed while removal claimed it; an unverified entry was "
                f"preserved at {error.preserved_path}, and its pending removal "
                "record was retained for recovery",
                retain_intent=True,
            ) from error
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
        paths.fsync_directory(target.parent)
    try:
        claim.discard()
    except OSError as error:
        try:
            restored = claim.restore()
        except OSError:
            restored = False
        if restored:
            with contextlib.suppress(OSError):
                paths.fsync_directory(target.parent)
            raise ManageError(
                "local-io",
                f"could not remove {path}; the original file was restored: "
                f"{error.strerror or error}",
            ) from error
        claim_survives = claim.is_present()
        source_is_original = False
        with contextlib.suppress(OSError, ValueError):
            source_is_original = file_io.regular_file_fingerprint(target) == expected_fingerprint
        if claim_survives or source_is_original:
            preserved = claim.path if claim_survives else path
            with contextlib.suppress(OSError):
                paths.fsync_directory(target.parent)
            raise ManageError(
                "local-io",
                f"could not finish removing {path}; the file remains preserved at "
                f"{preserved}, and its pending removal record was retained for recovery",
                retain_intent=True,
            ) from error
        physical = _completed_removal_outcome(
            item,
            authority_removed,
            remaining_artifacts,
            artifact_kept,
        )
        raise ManageError(
            "local-io",
            f"removed {path}, but the final unlink outcome could not be confirmed"
            f"{physical.cleanup_note()}",
            committed=True,
            physical=physical,
        ) from error
    finally:
        with contextlib.suppress(OSError):
            claim.close()
    try:
        paths.fsync_directory(target.parent)
    except OSError as error:
        physical = _completed_removal_outcome(
            item,
            authority_removed,
            remaining_artifacts,
            artifact_kept,
        )
        raise ManageError(
            "local-io",
            f"removed {path}, but could not persist its removal: {error}{physical.cleanup_note()}",
            committed=True,
            physical=physical,
        ) from error
    return _completed_removal_outcome(
        item,
        authority_removed,
        remaining_artifacts,
        artifact_kept,
    )


def recover_removal_claim(
    path: Path,
    *,
    expected_source: SourceIdentity | None,
    expected_fingerprint: SourceFingerprint | None = None,
    operation_token: str,
    lookup_path: Path | None = None,
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
    target = path if lookup_path is None else lookup_path
    try:
        claim = file_io.recover_deletion_claim(
            target,
            expected_identity=expected_source,
            operation_token=operation_token,
            expected_fingerprint=expected_fingerprint,
            logical_path=path,
        )
    except (OSError, ValueError) as error:
        detail = str(error)
        if lookup_path is not None:
            detail = detail.replace(str(target.parent), str(path.parent))
        raise ManageError(
            "local-io",
            f"could not recover the pending removal claim for {path}: {detail}",
        ) from error
    if claim is None:
        return False
    try:
        claim.discard()
        paths.fsync_directory(target.parent)
    except OSError as error:
        raise ManageError(
            "local-io",
            f"could not finish the pending removal claim for {path}: {error}",
        ) from error
    finally:
        with contextlib.suppress(OSError):
            claim.close()
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


def _unlink_if_same(
    path: Path,
    identity: tuple[int, int],
    *,
    pinned_source: file_io.PinnedPath | None = None,
    retained_parent: Path,
) -> Path | None:
    """Remove only the directory entry this operation installed.

    The atomic claim is what makes "same" authoritative: a check followed by
    ``path.unlink()`` could otherwise delete a replacement installed between
    those two operations.
    """
    try:
        removed = file_io.discard_regular_if_same(
            path,
            expected_identity=identity,
            pinned_source=pinned_source,
            retained_parent=retained_parent,
            logical_retained_parent=retained_parent,
        )
    except file_io.PathChangedError as error:
        return error.preserved_path or path
    if not removed:
        return None
    try:
        paths.fsync_directory(path.parent)
    except OSError:
        # Rollback is best effort. Leaving an inert record or private claim is
        # safer than masking the original failure or unlinking a replacement.
        return None
    return None


def _with_trash_cleanup_conflict(
    error: ManageError,
    preserved: Path | None,
    *,
    entry: str = "trash-metadata entry",
) -> ManageError:
    """Keep a Trash failure's semantics while reporting retained authority."""
    if preserved is None:
        return error
    message = str(error.args[0]) if error.args else str(error)
    return ManageError(
        error.kind,
        f"{message}; additionally, an unverified {entry} was preserved at {preserved}",
        committed=error.committed,
        retain_intent=error.retain_intent,
        physical=error.physical,
    )


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
    expected_fingerprint: SourceFingerprint | None = None,
    prepared_pin: file_io.PinnedPath | None = None,
    lookup_path: Path | None = None,
    source_root: Path | None = None,
    lookup_root: Path | None = None,
    lookup_parent: Path | None = None,
    source_context: file_io.PinnedDirectoryContext | None = None,
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
    target = path if lookup_path is None else lookup_path
    try:
        _validate_source_context(
            path,
            prepared_pin=prepared_pin,
            lookup_path=lookup_path,
            source_root=source_root,
            lookup_root=lookup_root,
            lookup_parent=lookup_parent,
            source_context=source_context,
        )
    except ManageError:
        if prepared_pin is not None:
            prepared_pin.close()
        raise
    if item.provider != "local" or not is_removable(item, roots):
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError(
            "not-ours",
            f"{path.name} lives outside your wallpaper folders, so it is not this app's to move",
        )
    if target.is_symlink() or not target.is_file():
        if prepared_pin is not None:
            prepared_pin.close()
        raise ManageError("missing", f"{path} is no longer there")
    try:
        source_pin = _require_source_pin(
            path,
            expected_source,
            expected_fingerprint,
            prepared_pin,
            lookup_path=lookup_path,
        )
    except ManageError:
        if prepared_pin is not None:
            prepared_pin.close()
        raise
    try:
        if expected_fingerprint is None:  # narrowed by _require_source_pin
            raise AssertionError("source fingerprint validation did not narrow")
        committed = False
        result: Trashed | None = None
        try:
            with (
                stills.media_path_lifecycle_lock(path),
                stills.source_lifecycle_lock(item),
                contextlib.ExitStack() as access_stack,
            ):
                artifact_pins, artifact_kept = _pin_pairing_artifacts_locked(
                    item,
                    tuple(roots),
                    legacy_selected_still=None,
                    source_root=source_root,
                    lookup_root=lookup_root,
                    lookup_parent=lookup_parent,
                    source_context=source_context,
                    access_stack=access_stack,
                )
                result = _trash_pinned(
                    item,
                    source_pin=source_pin,
                    expected_fingerprint=expected_fingerprint,
                    source_path=target,
                    artifact_pins=artifact_pins,
                    artifact_kept=artifact_kept,
                )
                committed = True
            if result is None:  # pragma: no cover - successful helper always returns
                raise AssertionError("committed trash move produced no physical outcome")
            return result
        except OSError as error:
            raise _cleanup_error(
                error,
                path=path,
                committed=committed,
                physical=result,
            ) from error
    finally:
        _release_source_pins(source_pin, prepared_pin)


def _trash_pinned(
    item: MediaItem,
    *,
    source_pin: file_io.PinnedPath,
    expected_fingerprint: SourceFingerprint,
    source_path: Path,
    artifact_pins: Sequence[_PinnedArtifact],
    artifact_kept: Sequence[Path],
) -> Trashed:
    """Commit one trash move while its prepare-time inode stays pinned."""
    path = item.path
    target = source_path
    authority_removed, remaining_artifacts = _withdraw_download_authority(
        item,
        artifact_pins,
        required=False,
        source_pin=source_pin,
        expected_fingerprint=expected_fingerprint,
    )

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
    original_identity = source_pin.identity
    original_fingerprint = expected_fingerprint
    stamp = datetime.now().replace(microsecond=0).isoformat()
    payload = f"[Trash Info]\nPath={quote(str(original), safe='/')}\nDeletionDate={stamp}\n"
    payload_bytes = payload.encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".wall-in-one-trashinfo-", dir=info)
    temporary = Path(temporary_name)
    temporary_status = os.fstat(descriptor)
    temporary_identity = temporary_status.st_dev, temporary_status.st_ino
    try:
        temporary_pin = file_io.pin_regular_path(
            temporary,
            expected_identity=temporary_identity,
        )
    except OSError as error:
        os.close(descriptor)
        raise ManageError(
            "local-io", f"could not secure the trash record: {error.strerror or error}"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
    except OSError as error:
        try:
            preserved = _unlink_if_same(
                temporary,
                temporary_identity,
                pinned_source=temporary_pin,
                retained_parent=info.parent,
            )
        finally:
            temporary_pin.close()
        failure = ManageError(
            "local-io", f"could not write the trash record: {error.strerror or error}"
        )
        raise _with_trash_cleanup_conflict(
            failure,
            preserved,
            entry="temporary trash-metadata entry",
        ) from error

    move_committed = False
    physical_outcome: Trashed | None = None
    record_pin: file_io.PinnedPath | None = None
    try:
        for name in _trash_names(original):
            if record_pin is not None:
                raise AssertionError("a prior trash record pin was not released")
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
                record_pin = file_io.pin_regular_path(
                    record,
                    expected_identity=record_identity,
                )
                _require_exact_trash_record(record, record_pin, payload_bytes)
            except OSError as error:
                preserved = (
                    _unlink_if_same(
                        record,
                        record_identity,
                        pinned_source=record_pin,
                        retained_parent=info.parent,
                    )
                    if record_pin is not None
                    else record
                )
                failure = ManageError(
                    "changed",
                    f"the reserved trash record {record} did not contain the exact "
                    "staged metadata; the source was not moved",
                )
                raise _with_trash_cleanup_conflict(failure, preserved) from error
            try:
                paths.fsync_directory(info)
            except OSError as error:
                preserved = _unlink_if_same(
                    record,
                    record_identity,
                    pinned_source=record_pin,
                    retained_parent=info.parent,
                )
                failure = ManageError(
                    "local-io",
                    f"could not persist the trash record: {error.strerror or error}",
                )
                raise _with_trash_cleanup_conflict(failure, preserved) from error

            destination = files / name
            try:
                _require_exact_trash_record(record, record_pin, payload_bytes)
            except OSError as error:
                preserved = _unlink_if_same(
                    record,
                    record_identity,
                    pinned_source=record_pin,
                    retained_parent=info.parent,
                )
                failure = ManageError(
                    "changed",
                    f"the reserved trash record {record} changed before the media "
                    "move; the source was not moved",
                )
                raise _with_trash_cleanup_conflict(failure, preserved) from error
            try:
                file_io.atomic_move_no_replace(
                    target,
                    destination,
                    expected_identity=original_identity,
                    expected_fingerprint=original_fingerprint,
                    pinned_source=source_pin,
                )
            except FileExistsError as error:
                preserved = _unlink_if_same(
                    record,
                    record_identity,
                    pinned_source=record_pin,
                    retained_parent=info.parent,
                )
                record_pin.close()
                record_pin = None
                if preserved is not None:
                    failure = ManageError(
                        "local-io",
                        f"could not withdraw the trash record reserved for {destination}",
                    )
                    raise _with_trash_cleanup_conflict(failure, preserved) from error
                continue
            except file_io.PathChangedError as error:
                # If restoration was blocked, the moved entry remains in the
                # trash and its already-durable record must stay with it.
                if error.preserved_path is not None:
                    physical_outcome = _completed_trash_outcome(
                        item,
                        error.preserved_path,
                        authority_removed,
                        remaining_artifacts,
                        artifact_kept,
                    )
                    raise ManageError(
                        "changed",
                        f"{original} changed while it was being moved; the unverified "
                        f"entry was preserved at {error.preserved_path}"
                        f"{physical_outcome.cleanup_note()}",
                        committed=True,
                        physical=physical_outcome,
                    ) from error
                preserved = _unlink_if_same(
                    record,
                    record_identity,
                    pinned_source=record_pin,
                    retained_parent=info.parent,
                )
                failure = ManageError(
                    "changed",
                    f"{original} was replaced while it was being moved; nothing was deleted",
                )
                raise _with_trash_cleanup_conflict(failure, preserved) from error
            except OSError as error:
                preserved = _unlink_if_same(
                    record,
                    record_identity,
                    pinned_source=record_pin,
                    retained_parent=info.parent,
                )
                if error.errno == errno.EXDEV:
                    failure = ManageError(
                        "cross-device",
                        f"{original.name} is on another filesystem, "
                        "so it cannot be moved to the trash",
                    )
                elif error.errno == errno.ENOENT:
                    failure = ManageError("missing", f"{original} is no longer there")
                else:
                    failure = ManageError(
                        "local-io", f"could not move {original}: {error.strerror or error}"
                    )
                raise _with_trash_cleanup_conflict(failure, preserved) from error

            move_committed = True
            try:
                paths.fsync_directory(files)
                paths.fsync_directory(target.parent)
            except OSError as error:
                # The atomic move already removed the source. Rolling back the
                # trash copy here would be data loss, so keep it and report the
                # durability uncertainty explicitly.
                physical_outcome = _completed_trash_outcome(
                    item,
                    destination,
                    authority_removed,
                    remaining_artifacts,
                    artifact_kept,
                )
                raise ManageError(
                    "local-io",
                    f"moved {original}, but could not persist the move: {error}"
                    f"{physical_outcome.cleanup_note()}",
                    committed=True,
                    physical=physical_outcome,
                ) from error
            try:
                _require_exact_trash_record(record, record_pin, payload_bytes)
            except OSError as error:
                # The media move cannot be rolled back without risking data
                # loss. Preserve the unverified record name, consume every
                # pre-pinned companion, and expose the exact physical outcome.
                physical_outcome = _completed_trash_outcome(
                    item,
                    destination,
                    authority_removed,
                    remaining_artifacts,
                    artifact_kept,
                )
                raise ManageError(
                    "changed",
                    f"moved {original}, but its trash record changed at {record}; "
                    "the unverified record was preserved"
                    f"{physical_outcome.cleanup_note()}",
                    committed=True,
                    physical=physical_outcome,
                ) from error
            # The source move is the commit point.  From here, discard only
            # paths which are independently proven to have been generated for
            # this item.  A custom representative still is never among them.
            physical_outcome = _completed_trash_outcome(
                item,
                destination,
                authority_removed,
                remaining_artifacts,
                artifact_kept,
            )
            return physical_outcome
        raise ManageError(
            "local-io", f"the trash already holds {MAX_TRASH_ATTEMPTS} files so named"
        )
    finally:
        active_error = sys.exception()
        try:
            if record_pin is not None:
                record_pin.close()
        finally:
            try:
                preserved = _unlink_if_same(
                    temporary,
                    temporary_identity,
                    pinned_source=temporary_pin,
                    retained_parent=info.parent,
                )
            finally:
                temporary_pin.close()
        if preserved is not None:
            if isinstance(active_error, ManageError):
                raise _with_trash_cleanup_conflict(
                    active_error,
                    preserved,
                    entry="temporary trash-metadata entry",
                ) from active_error
            if active_error is None:
                action = (
                    f"moved {original} to the trash"
                    if move_committed
                    else f"could not finish moving {original} to the trash"
                )
                raise ManageError(
                    "local-io",
                    f"{action}; an unverified temporary trash-metadata entry was "
                    f"preserved at {preserved}",
                    committed=move_committed,
                    physical=physical_outcome,
                )
            active_error.add_note(
                f"an unverified temporary trash-metadata entry was preserved at {preserved}"
            )
