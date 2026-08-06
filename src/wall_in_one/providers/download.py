"""Where a download lands, and how it gets there without eating anything.

Two files prove a wallpaper is ours to delete, and `library.scan` demands
both: a **directory marker** saying we created the directory, and a **per-file
sidecar** saying we downloaded that particular file. A marker alone is not
enough, which is what keeps a file the user dropped into a managed directory
by hand out of reach of the delete button.

The names here are therefore not free -- they have to be the ones
`library.scan._DIRECTORY_MARKERS` and `_FILE_SIDECAR_SUFFIXES` already look
for, and they are the same names the predecessor wrote, so an existing library
keeps its ownership across the rewrite.

Installation is `os.link` from a staged temporary in the same directory, not
`os.replace`. Both are atomic; `link` additionally *fails* when the destination
exists, and never overwriting a file we did not create is the whole point of
the ownership rules above. The predecessor's suite has a regression test for
exactly that (`test_preexisting_media_is_not_replaced`), and it is ported.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one.providers.base import ProviderError

#: Everything this app downloads lives under one directory in the user's
#: wallpaper root, so a whole install is one directory to inspect or delete.
MANAGED_PARENT: Final = "Wall-in-One"

#: A sidecar is a few hundred bytes; this only stops a bug writing a novel.
MAX_SIDECAR_BYTES: Final = 64 * 1024

#: Enough distinct names that a collision means something is wrong, few enough
#: that the loop terminates promptly.
MAX_NAME_ATTEMPTS: Final = 10_000


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
    directory = root / MANAGED_PARENT / location.directory_name
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not create {directory}: {error.strerror or error}"
        ) from error
    return directory, _write_marker(directory, location)


def _write_marker(directory: Path, location: ManagedLocation) -> Path:
    marker = directory / location.marker_name
    if marker.is_symlink():
        # Nothing legitimate makes this a symlink, and following one would let
        # a download's ownership claim be redirected at an arbitrary file.
        raise ProviderError("invalid-path", f"ownership marker is a symlink: {marker}")
    payload = encode_sidecar(location.marker_payload)
    if marker.is_file():
        try:
            existing = marker.read_bytes()
        except OSError:
            existing = b""
        if existing == payload:
            return marker
        try:
            document: object = json.loads(existing)
        except ValueError:
            document = None
        if isinstance(document, dict) and document.get("schema") == 1:
            # Someone else's version of our marker, or an older one. It still
            # says the directory is managed, which is all that is claimed.
            return marker
    _atomic_write(marker, payload)
    return marker


def unique_destination(directory: Path, stem: str, extension: str, sidecar_suffix: str) -> Path:
    """First free ``<stem>.<ext>`` in ``directory``, counting up on collisions.

    A name is only free when the media file *and* its sidecar are both absent:
    a stray sidecar with no media means an interrupted install, and reusing
    that name would attach the wrong provenance to a new download.
    """
    for attempt in range(MAX_NAME_ATTEMPTS):
        name = f"{stem}{extension}" if attempt == 0 else f"{stem}-{attempt}{extension}"
        candidate = safe_child(directory, name)
        if not os.path.lexists(candidate) and not os.path.lexists(str(candidate) + sidecar_suffix):
            return candidate
    raise ProviderError("conflict", f"could not allocate a free name for {stem}")


def install(
    staged: Path, destination: Path, sidecar_suffix: str, sidecar_payload: bytes
) -> tuple[Path, Path]:
    """Move ``staged`` into place next to a freshly written sidecar.

    Both links are no-replace, and either one failing rolls the other back, so
    the library never sees a media file without its provenance or the reverse.
    ``staged`` must already be in ``destination``'s directory -- it is, because
    the transport streams downloads into the directory they are destined for,
    which is also what makes `os.link` cheap and same-filesystem by
    construction.
    """
    directory = destination.parent
    if staged.parent != directory:
        raise ProviderError("invalid-path", "staged download is not in its destination directory")
    sidecar_destination = Path(str(destination) + sidecar_suffix)

    descriptor, name = tempfile.mkstemp(prefix=".wall-in-one-tmp-", dir=directory)
    sidecar_temporary = Path(name)
    installed_media = False
    installed_sidecar = False
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(sidecar_payload)
            sink.flush()
            os.fsync(sink.fileno())
        os.link(staged, destination, follow_symlinks=False)
        installed_media = True
        os.link(sidecar_temporary, sidecar_destination, follow_symlinks=False)
        installed_sidecar = True
    except FileExistsError as error:
        _roll_back(installed_media, installed_sidecar, destination, sidecar_destination)
        sidecar_temporary.unlink(missing_ok=True)
        raise ProviderError(
            "conflict", f"{error.filename} appeared before it could be installed"
        ) from error
    except OSError as error:
        _roll_back(installed_media, installed_sidecar, destination, sidecar_destination)
        sidecar_temporary.unlink(missing_ok=True)
        raise ProviderError(
            "local-io", f"could not install download: {error.strerror or error}"
        ) from error
    staged.unlink(missing_ok=True)
    sidecar_temporary.unlink(missing_ok=True)
    return destination, sidecar_destination


def _roll_back(installed_media: bool, installed_sidecar: bool, media: Path, sidecar: Path) -> None:
    if installed_sidecar:
        sidecar.unlink(missing_ok=True)
    if installed_media:
        media.unlink(missing_ok=True)


def _atomic_write(destination: Path, payload: bytes) -> None:
    """Write ``payload`` to ``destination`` via a temporary in the same directory."""
    descriptor, name = tempfile.mkstemp(prefix=".wall-in-one-tmp-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ProviderError(
            "local-io", f"could not write {destination}: {error.strerror or error}"
        ) from error
