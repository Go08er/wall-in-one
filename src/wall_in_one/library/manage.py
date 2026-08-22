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

import errno
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote

from wall_in_one import paths
from wall_in_one.library import pairing, scan
from wall_in_one.library.model import Kind, MediaItem

#: Where the freedesktop home trash lives, relative to the data home.
TRASH_DIRECTORY: Final = "Trash"

#: How many `name (n)` variants to try before giving up on a unique name.
MAX_TRASH_ATTEMPTS: Final = 1000


class ManageError(Exception):
    """A file could not be removed, with a machine-readable reason.

    Kinds in use: ``not-ours``, ``missing``, ``outside-root``, ``symlink``,
    ``cross-device``, ``local-io``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

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


def _companions(item: MediaItem, roots: tuple[Path, ...]) -> list[Path]:
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

    pairing_sidecar = item.path.with_name(item.path.name + pairing.SIDECAR_SUFFIX)
    if pairing_sidecar.is_file() and not pairing_sidecar.is_symlink():
        found.append(pairing_sidecar)

    if item.kind is not Kind.VIDEO:
        return found
    still = item.paired_still
    if still is None or still.is_symlink() or not still.is_file():
        return found
    generated = still.stem == pairing.automatic_still_stem(item.path) and any(
        still.parent == pairing.still_directory(root) for root in roots
    )
    if generated:
        found.append(still)
        sidecar = still.with_name(still.name + pairing.SIDECAR_SUFFIX)
        if sidecar.is_file() and not sidecar.is_symlink():
            found.append(sidecar)
    return found


def remove(item: MediaItem, roots: tuple[Path, ...] = ()) -> Removal:
    """Delete a wallpaper we downloaded, and everything we wrote beside it.

    Refuses anything else. The refusal is the point of the function: the app
    writes into a directory the user also keeps their own wallpapers in, and
    the only thing standing between a delete button and somebody's photographs
    is this check.
    """
    path = item.path
    if item.provider == scan.WORKSHOP_PROVIDER or item.kind is Kind.SCENE:
        raise ManageError("not-ours", f"{path.name} belongs to Steam, not this app")
    if not roots or not _within(path, roots):
        raise ManageError("outside-root", f"{path} is not inside the library")
    if path.is_symlink():
        raise ManageError("symlink", f"{path} is a symbolic link, so it is not ours to delete")
    if not path.is_file():
        raise ManageError("missing", f"{path} is no longer there")
    if not _is_managed_on_disk(path):
        # Deliberately phrased as ours-or-not rather than as a permission
        # problem: the user has every right to delete this file, just not
        # through us.
        raise ManageError(
            "not-ours",
            f"{path.name} is your own file, not one this app downloaded",
        )

    removed: list[Path] = []
    kept: list[Path] = []
    # The wallpaper goes first. If the run stops after this, what is left
    # behind is inert metadata rather than a wallpaper with no sidecar, which
    # would read as an unmanaged file the next time anything looked.
    try:
        path.unlink()
    except OSError as error:
        raise ManageError("local-io", f"could not remove {path}: {error.strerror or error}") from (
            error
        )
    removed.append(path)

    for companion in _companions(item, roots):
        try:
            companion.unlink()
        except OSError:
            kept.append(companion)
        else:
            removed.append(companion)
    return Removal(item=item, removed=tuple(removed), kept=tuple(kept))


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
    """Remove only the directory entry this operation installed."""
    try:
        current = _identity(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    if current != identity:
        return
    try:
        path.unlink()
        paths.fsync_directory(path.parent)
    except OSError:
        # Rollback is best effort. Leaving an extra hard link or inert record
        # is safer than masking the original failure or unlinking a replacement.
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


def trash(item: MediaItem, roots: Sequence[Path] = ()) -> Path:
    """Move ``item`` into the trash and return where it landed.

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
    stamp = datetime.now().replace(microsecond=0).isoformat()
    payload = f"[Trash Info]\nPath={quote(str(original), safe='/')}\nDeletionDate={stamp}\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".wall-in-one-trashinfo-", dir=info)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ManageError(
            "local-io", f"could not write the trash record: {error.strerror or error}"
        ) from error

    temporary_identity = _identity(temporary)
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
                os.link(original, destination, follow_symlinks=False)
            except FileExistsError:
                _unlink_if_same(record, record_identity)
                continue
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

            destination_identity = _identity(destination)
            if destination_identity != original_identity:
                _unlink_if_same(destination, destination_identity)
                _unlink_if_same(record, record_identity)
                raise ManageError("local-io", f"{original} changed while it was being moved")
            try:
                paths.fsync_directory(files)
                if _identity(original) != original_identity:
                    raise ManageError("local-io", f"{original} changed while it was being moved")
                original.unlink()
            except (OSError, ManageError) as error:
                _unlink_if_same(destination, destination_identity)
                _unlink_if_same(record, record_identity)
                if isinstance(error, ManageError):
                    raise
                kind = "missing" if error.errno == errno.ENOENT else "local-io"
                raise ManageError(kind, f"could not finish moving {original}: {error}") from error
            try:
                paths.fsync_directory(original.parent)
            except OSError as error:
                # The source is already gone, so rolling back the durable trash
                # copy here would be data loss. Keep it and report uncertainty.
                raise ManageError(
                    "local-io",
                    f"moved {original}, but could not persist its removal: {error}",
                ) from error
            return destination
        raise ManageError(
            "local-io", f"the trash already holds {MAX_TRASH_ATTEMPTS} files so named"
        )
    finally:
        _unlink_if_same(temporary, temporary_identity)
