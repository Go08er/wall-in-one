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
    return scan.is_managed_directory(path.parent) and scan.has_file_sidecar(path)


def _companions(item: MediaItem, roots: tuple[Path, ...]) -> list[Path]:
    """Everything we wrote beside ``item`` and should take with it.

    Only files whose names we generate: the download sidecar, the pairing
    sidecar, and a still that lives in the managed `Automatic Stills`
    directory. A still the user made themselves and named by convention is
    theirs and stays, even though it is about to have nothing to pair with.
    """
    found: list[Path] = []
    for suffix in scan.FILE_SIDECAR_SUFFIXES:
        candidate = item.path.with_name(item.path.name + suffix)
        if candidate.is_file() and not candidate.is_symlink():
            found.append(candidate)

    if item.kind is not Kind.VIDEO:
        return found
    still = item.paired_still
    if still is None or still.is_symlink() or not still.is_file():
        return found
    generated = any(still.parent == pairing.still_directory(root) for root in roots)
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
    if roots and not _within(path, roots):
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


def _trash_name(directory: Path, original: Path) -> str:
    """A name free in ``directory``, keeping the extension recognisable.

    The suffix goes before the extension so that a restored `foo (1).mp4` is
    still obviously a video, which `foo.mp4 (1)` would not be.
    """
    if not (directory / original.name).exists():
        return original.name
    for attempt in range(1, MAX_TRASH_ATTEMPTS):
        candidate = f"{original.stem} ({attempt}){original.suffix}"
        if not (directory / candidate).exists():
            return candidate
    raise ManageError("local-io", f"the trash already holds {MAX_TRASH_ATTEMPTS} files so named")


def is_removable(path: Path, roots: Sequence[Path] = ()) -> bool:
    """Whether this app may move ``path`` at all.

    Inside a configured root, or nowhere. Being in the library is not enough:
    Steam's Workshop content is scanned into the library and is emphatically
    not the app's to move.
    """
    return not roots or _within(path, tuple(roots))


def trash(path: Path, roots: Sequence[Path] = ()) -> Path:
    """Move ``path`` into the trash and return where it landed.

    The reversible verb, and therefore the right one for a file the user made.
    Only the home trash is implemented: a wallpaper on another filesystem
    cannot be renamed into it, and the specification's per-device `.Trash-$uid`
    fallback is a second mechanism with its own failure modes. Saying so is
    better than silently unlinking something the user expected to be able to
    get back.
    """
    if not is_removable(path, roots):
        raise ManageError(
            "not-ours",
            f"{path.name} lives outside your wallpaper folders, so it is not this app's to move",
        )
    if path.is_symlink() or not path.exists():
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
    name = _trash_name(files, original)
    # The record is written first. A record with no file is a stale entry that
    # a file manager ignores; a file with no record is a file the user cannot
    # restore, which is the failure that matters.
    record = info / f"{name}.trashinfo"
    stamp = datetime.now().replace(microsecond=0).isoformat()
    payload = f"[Trash Info]\nPath={quote(str(original), safe='/')}\nDeletionDate={stamp}\n"
    try:
        record.write_text(payload, encoding="utf-8")
    except OSError as error:
        raise ManageError(
            "local-io", f"could not write the trash record: {error.strerror or error}"
        ) from error

    destination = files / name
    try:
        os.rename(original, destination)
    except OSError as error:
        record.unlink(missing_ok=True)
        if error.errno == errno.EXDEV:
            raise ManageError(
                "cross-device",
                f"{original.name} is on another filesystem, so it cannot be moved to the trash",
            ) from error
        raise ManageError(
            "local-io", f"could not move {original} to the trash: {error.strerror or error}"
        ) from error
    return destination
