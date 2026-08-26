"""Walking the wallpaper roots.

Bounded on purpose. A wallpaper root is a user directory and can be pointed at
anything -- a home directory, a network mount, a symlink loop -- so the walk
has a ceiling on entries examined, a depth limit, and refuses to follow
directory symlinks. Hitting a limit is reported in `Library.skipped` rather
than raised: a partial library is more useful than no library.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import file_io, paths
from wall_in_one.library import adopted, pairing, pairings, workshop
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership, classify

#: What a Wallpaper Engine wallpaper is labelled as in the grid.
WORKSHOP_PROVIDER: Final = "Wallpaper Engine"

#: Ceilings for one scan.
MAX_ITEMS: Final = 4096
MAX_ENTRIES_EXAMINED: Final = 65536
MAX_DEPTH: Final = 8
MAX_NOCTALIA_SETTINGS_BYTES: Final = 8 * 1024 * 1024


class ScanCancelledError(Exception):
    """A superseded background scan stopped at a cooperative boundary."""


#: A directory carrying one of these was created by us, so files inside it may
#: be deletable -- but only with a per-file sidecar to prove which download
#: they came from. Directory marker alone is never enough.
_DIRECTORY_MARKERS: Final[tuple[str, ...]] = (
    ".managed-by-wall-in-one-v1.json",
    ".wall-in-one-motionbgs-managed.json",
)

#: Provider provenance sidecars that grant deletion authority when paired with
#: a managed-directory marker. Pairing metadata is intentionally absent: a
#: user can customise a file without turning it into one of our downloads.
_DOWNLOAD_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = (
    ".motionbgs.json",
    ".wallhaven.json",
)

#: Every per-media sidecar recognised by the scanner. This broader set is for
#: filtering metadata out of the library and companion cleanup only; it must
#: never be used as an ownership predicate.
_MEDIA_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = (
    *_DOWNLOAD_SIDECAR_SUFFIXES,
    pairing.SIDECAR_SUFFIX,
)


@dataclass(frozen=True, slots=True)
class DownloadAuthority:
    """A provider authority document carrying generation and digest bindings."""

    provider: str
    size: int
    sha256: str
    generation: file_io.FileFingerprint

    def matches_generation(self, fingerprint: file_io.FileFingerprint) -> bool:
        return fingerprint == self.generation


def _read_marker(directory: Path) -> tuple[str, dict[str, object]] | None:
    for name in _DIRECTORY_MARKERS:
        marker = directory / name
        try:
            raw = file_io.read_regular_bytes(marker, pairing.MAX_SIDECAR_BYTES)
            if raw is None:
                continue
            document = json.loads(raw)
        except OSError, ValueError, RecursionError:
            continue
        if (
            not isinstance(document, dict)
            or type(document.get("schema")) is not int
            or document.get("schema") != 1
        ):
            continue
        if name == ".wall-in-one-motionbgs-managed.json":
            # The first MotionBGS marker used ``owner`` rather than ``plugin``;
            # accepting that exact identity is the only legacy allowance.
            owner = document.get("plugin", document.get("owner"))
            provider = document.get("provider", "MotionBGS")
            if owner == "goober/wall-in-one" and provider == "MotionBGS":
                return "MotionBGS", document
        elif (
            document.get("kind") == "wallhaven"
            and document.get("ownership") == "managed"
            and document.get("plugin", "goober/wall-in-one") == "goober/wall-in-one"
            and document.get("provider", "Wallhaven") == "Wallhaven"
        ):
            # The predecessor's Wallhaven marker had schema/kind/ownership but
            # no plugin/provider fields.  Those three exact legacy fields are
            # still a defensible app-created identity; arbitrary JSON is not.
            return "Wallhaven", document
    return None


def download_provenance(
    path: Path,
    *,
    expected_path: Path | None = None,
) -> str | None:
    """Provider proven by an exact, adjacent provenance sidecar.

    A suffix is a naming convention, not deletion authority. New records bind
    the provider identity, logical path, exact media generation and validated
    digest. Unbound predecessor records fail closed as local files: accepting
    a bare identity document would let a crash-left sidecar attach to unrelated
    bytes which later appeared at the same pathname.
    """
    logical_path = path if expected_path is None else expected_path
    expected = {
        ".motionbgs.json": "MotionBGS",
        ".wallhaven.json": "Wallhaven",
    }
    for suffix, provider in expected.items():
        sidecar = path.with_name(path.name + suffix)
        try:
            raw = file_io.read_regular_bytes(sidecar, pairing.MAX_SIDECAR_BYTES)
            if raw is None:
                continue
        except OSError, ValueError, RecursionError:
            continue
        authority = download_authority_from_bytes(
            raw,
            expected_path=logical_path,
            expected_provider=provider,
        )
        if authority is None:
            continue
        try:
            with file_io.pin_regular_path(path) as media_pin:
                if authority.matches_generation(media_pin.fingerprint):
                    return authority.provider
        except OSError, ValueError:
            continue
    return None


def download_authority_from_bytes(
    raw: bytes,
    *,
    expected_path: Path,
    expected_provider: str,
) -> DownloadAuthority | None:
    """Parse one exact provenance document with media-generation authority."""
    try:
        document: object = json.loads(raw)
    except ValueError, RecursionError:
        return None
    if not (
        isinstance(document, dict)
        and type(document.get("schema")) is int
        and document.get("schema") == 1
        and document.get("plugin") == "goober/wall-in-one"
        and document.get("provider") == expected_provider
        and document.get("path") == str(expected_path)
    ):
        return None

    size = document.get("bytes")
    digest = document.get("sha256")
    generation = document.get("media_generation")
    if (
        type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(generation, dict)
    ):
        return None
    device = generation.get("device")
    inode = generation.get("inode")
    generation_size = generation.get("bytes")
    mtime_ns = generation.get("mtime_ns")
    ctime_ns = generation.get("ctime_ns")
    if not all(
        type(value) is int for value in (device, inode, generation_size, mtime_ns, ctime_ns)
    ):
        return None
    assert isinstance(device, int)
    assert isinstance(inode, int)
    assert isinstance(generation_size, int)
    assert isinstance(mtime_ns, int)
    assert isinstance(ctime_ns, int)
    if device < 0 or inode < 0 or generation_size != size:
        return None
    return DownloadAuthority(
        provider=expected_provider,
        size=size,
        sha256=digest,
        generation=(device, inode, generation_size, mtime_ns, ctime_ns),
    )


def download_provenance_from_bytes(
    raw: bytes,
    *,
    expected_path: Path,
    expected_provider: str,
) -> bool:
    """Whether exact sidecar bytes carry generation-bound authority."""
    return (
        download_authority_from_bytes(
            raw,
            expected_path=expected_path,
            expected_provider=expected_provider,
        )
        is not None
    )


def _has_download_sidecar(path: Path) -> bool:
    return download_provenance(path) is not None


def _is_sidecar(path: Path) -> bool:
    name = path.name
    return name in _DIRECTORY_MARKERS or any(
        name.endswith(suffix) for suffix in _MEDIA_SIDECAR_SUFFIXES
    )


#: The two halves of ownership, published for `library.manage`. Deletion has to
#: ask the same question a scan asks, and asking it with a second copy of the
#: rule is how the two come to disagree -- with an unlink on the losing side.
MEDIA_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = _MEDIA_SIDECAR_SUFFIXES


def is_managed_directory(directory: Path) -> bool:
    """Whether ``directory`` carries a marker saying this app created it."""
    return _read_marker(directory) is not None


def has_download_sidecar(
    path: Path,
    *,
    expected_path: Path | None = None,
) -> bool:
    """Whether ``path`` has provider provenance proving it was downloaded."""
    return download_provenance(path, expected_path=expected_path) is not None


def wallpaper_directory_from_noctalia() -> Path | None:
    """Read `[wallpaper] directory` out of Noctalia's settings.

    Reusing Noctalia's own configured directory means the two agree about what
    the library is without the user configuring it twice.
    """
    try:
        raw = file_io.read_regular_bytes(
            paths.noctalia_settings_path(), MAX_NOCTALIA_SETTINGS_BYTES
        )
        if raw is None:
            return None
        document = tomllib.loads(raw.decode("utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError:
        return None
    section = document.get("wallpaper")
    if not isinstance(section, dict):
        return None
    directory = section.get("directory")
    if not isinstance(directory, str) or not directory:
        return None
    candidate = Path(directory).expanduser()
    return candidate if candidate.is_dir() else None


def default_roots() -> tuple[Path, ...]:
    """Where to look when the user has not said."""
    from_noctalia = wallpaper_directory_from_noctalia()
    if from_noctalia is not None:
        return (from_noctalia,)
    for candidate in (Path.home() / "Pictures" / "Wallpapers", Path.home() / "Pictures"):
        if candidate.is_dir():
            return (candidate,)
    return ()


def _walk(
    root: Path,
    budget: list[int],
    skipped: list[str],
    cancelled: Callable[[], bool] | None = None,
) -> Iterable[Path]:
    """Yield candidate files under ``root``, depth-first and bounded."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if cancelled is not None and cancelled():
            raise ScanCancelledError
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            skipped.append(f"{directory}: {error.strerror or error}")
            continue
        for entry in entries:
            if cancelled is not None and cancelled():
                raise ScanCancelledError
            budget[0] -= 1
            if budget[0] <= 0:
                skipped.append(f"{root}: stopped after {MAX_ENTRIES_EXAMINED} entries")
                return
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 > MAX_DEPTH:
                        skipped.append(f"{entry.path}: deeper than {MAX_DEPTH} levels")
                        continue
                    stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError as error:
                skipped.append(f"{entry.path}: {error.strerror or error}")


def workshop_items(
    steam_roots: Sequence[Path] | None = None,
    *,
    content_directories: Sequence[Path] | None = None,
) -> tuple[MediaItem, ...]:
    """Installed Wallpaper Engine wallpapers, videos and scenes alike.

    `Ownership.USER` without exception: these are Steam's files, in Steam's
    directories, and `library.manage` must refuse to touch one however managed
    the surrounding tree looks.
    """
    found: list[MediaItem] = []
    if steam_roots is not None and content_directories is not None:
        raise ValueError("Workshop scan cannot mix Steam roots and exact content directories")
    if content_directories is not None:
        installed = workshop.scan_content_directories(content_directories)
    elif steam_roots is None:
        installed = workshop.scan()
    else:
        installed = workshop.scan(steam_roots, include_defaults=False)
    for item in installed:
        entry = item.entry
        if item.is_video and entry is not None:
            try:
                info = entry.stat()
            except OSError:
                continue
            found.append(
                MediaItem(
                    path=entry,
                    kind=Kind.VIDEO,
                    size=info.st_size,
                    mtime=int(info.st_mtime),
                    ownership=Ownership.USER,
                    provider=WORKSHOP_PROVIDER,
                    title=item.title,
                )
            )
        elif item.kind == "scene":
            # No file to point at: the content is inside `scene.pkg`, so the
            # path is the directory and the Workshop id is what the renderer
            # is given. Size is the directory's own, which is meaningless as a
            # wallpaper size but keeps "largest first" from putting every
            # scene at one end of the grid.
            try:
                info = item.directory.stat()
            except OSError:
                continue
            found.append(
                MediaItem(
                    path=item.directory,
                    kind=Kind.SCENE,
                    size=info.st_size,
                    mtime=int(info.st_mtime),
                    ownership=Ownership.USER,
                    provider=WORKSHOP_PROVIDER,
                    scene=item.id,
                    title=item.title,
                    preview=item.preview,
                )
            )
    return tuple(found)


def scan(
    roots: Sequence[Path] | None = None,
    records: Mapping[str, pairings.Pairing] | None = None,
    *,
    include_workshop: bool = False,
    workshop_roots: Sequence[Path] | None = None,
    workshop_content_directories: Sequence[Path] | None = None,
    adoption_candidates: Sequence[adopted.Authority] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Library:
    """Build a `Library` from ``roots`` (or the default roots).

    ``records`` are the stored pairing customizations. Passed in rather than
    read here, because a scan should not have opinions about whose choices it
    is honouring -- and because pairing has to happen exactly once. Resolving
    a second time somewhere else would recompute the defaults from disk and
    overwrite whatever this pass decided.
    """
    resolved_roots = tuple(roots) if roots is not None else default_roots()
    if adoption_candidates is None:
        capture_adoption = adopted.load(strict=False)
        if capture_adoption is not None and capture_adoption.root in resolved_roots:
            candidate_authorities = capture_adoption.authorities
        else:
            candidate_authorities = ()
    else:
        # The deployed-profile detector uses its still-open generation proof
        # to dry-render schema 4 before publishing the adoption manifest.  An
        # injected candidate is not trusted on identity alone: the same
        # source/capture generation, owner, and single-link constraints below
        # are required exactly as they are for a persisted manifest.
        candidate_authorities = tuple(adoption_candidates)

    budget = [MAX_ENTRIES_EXAMINED]
    skipped: list[str] = []
    items: list[MediaItem] = []
    seen: set[Path] = set()
    observed_generations: dict[
        Path,
        tuple[file_io.FileFingerprint, int, int],
    ] = {}
    marker_cache: dict[Path, tuple[str, dict[str, object]] | None] = {}

    for root in resolved_roots:
        if cancelled is not None and cancelled():
            raise ScanCancelledError
        if root.is_symlink() or not root.is_dir():
            skipped.append(f"{root}: not a directory")
            continue
        for path in _walk(root, budget, skipped, cancelled):
            if len(items) >= MAX_ITEMS:
                skipped.append(f"{root}: stopped at {MAX_ITEMS} wallpapers")
                break
            if path in seen or _is_sidecar(path):
                continue
            kind = classify(path)
            if kind is None:
                continue
            try:
                info = path.stat()
                generation = file_io.file_fingerprint(info)
            except OSError, ValueError:
                continue
            seen.add(path)
            observed_generations[path] = (generation, info.st_uid, info.st_nlink)

            directory = path.parent
            if directory not in marker_cache:
                marker_cache[directory] = _read_marker(directory)
            marker = marker_cache[directory]

            ownership = Ownership.USER
            provider = "local"
            # A directory marker says we created the directory; the per-file
            # sidecar says we created *this file* and know where it came from.
            # Deletion needs both, so anything the user dropped into a managed
            # directory by hand stays theirs.
            provenance = download_provenance(path)
            if marker is not None and provenance == marker[0]:
                ownership = Ownership.MANAGED
                provider = provenance

            items.append(
                MediaItem(
                    path=path,
                    kind=kind,
                    size=info.st_size,
                    mtime=int(info.st_mtime),
                    ownership=ownership,
                    provider=provider,
                )
            )

    if cancelled is not None and cancelled():
        raise ScanCancelledError
    if include_workshop:
        # After the roots, so a wallpaper somebody has copied into their own
        # library wins over the Steam copy of it -- `seen` keeps the first.
        for item in workshop_items(
            workshop_roots,
            content_directories=workshop_content_directories,
        ):
            if cancelled is not None and cancelled():
                raise ScanCancelledError
            if item.path not in seen:
                seen.add(item.path)
                items.append(item)
                if item.kind is Kind.VIDEO:
                    try:
                        info = item.path.stat()
                        generation = file_io.file_fingerprint(info)
                    except OSError, ValueError:
                        pass
                    else:
                        observed_generations[item.path] = (
                            generation,
                            info.st_uid,
                            info.st_nlink,
                        )

    items.sort(key=lambda item: (item.path.parent.as_posix(), item.name.lower()))
    still_inventory = tuple(item for item in items if item.kind is Kind.STILL)
    exact_authorities = tuple(
        authority
        for authority in candidate_authorities
        if observed_generations.get(authority.source_path)
        == (authority.source_fingerprint, os.getuid(), 1)
        and observed_generations.get(authority.capture_path)
        == (authority.capture_fingerprint, os.getuid(), 1)
    )
    adopted_mapping = {
        authority.source_path: authority.capture_path for authority in exact_authorities
    }
    adopted_pairs = tuple(
        (authority.source_path, authority.capture_path) for authority in exact_authorities
    )
    if cancelled is not None and cancelled():
        raise ScanCancelledError
    paired = pairings.apply(
        items,
        resolved_roots,
        records,
        adopted_stills=adopted_mapping,
    )
    if cancelled is not None and cancelled():
        raise ScanCancelledError
    return Library(
        roots=resolved_roots,
        items=paired,
        skipped=tuple(skipped),
        still_inventory=still_inventory,
        adopted_stills=adopted_pairs,
    )
