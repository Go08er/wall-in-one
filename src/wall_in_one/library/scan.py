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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.library import pairing, pairings
from wall_in_one.library.model import Library, MediaItem, Ownership, classify

#: Ceilings for one scan.
MAX_ITEMS: Final = 4096
MAX_ENTRIES_EXAMINED: Final = 65536
MAX_DEPTH: Final = 8

#: A directory carrying one of these was created by us, so files inside it may
#: be deletable -- but only with a per-file sidecar to prove which download
#: they came from. Directory marker alone is never enough.
_DIRECTORY_MARKERS: Final[tuple[str, ...]] = (
    ".managed-by-wall-in-one-v1.json",
    ".wall-in-one-motionbgs-managed.json",
)

#: Per-file sidecars that grant deletion authority.
_FILE_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = (
    ".motionbgs.json",
    ".wallhaven.json",
    pairing.SIDECAR_SUFFIX,
)


def _read_marker(directory: Path) -> dict[str, object] | None:
    for name in _DIRECTORY_MARKERS:
        marker = directory / name
        try:
            if marker.stat().st_size > pairing.MAX_SIDECAR_BYTES:
                continue
            document = json.loads(marker.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(document, dict):
            return document
    return None


def _provider_of(marker: dict[str, object]) -> str:
    for key in ("provider", "kind"):
        value = marker.get(key)
        if isinstance(value, str) and value:
            return value
    return "Wall-in-One"


def _has_file_sidecar(path: Path) -> bool:
    return any(path.with_name(path.name + suffix).is_file() for suffix in _FILE_SIDECAR_SUFFIXES)


def _is_sidecar(path: Path) -> bool:
    name = path.name
    return name in _DIRECTORY_MARKERS or any(
        name.endswith(suffix) for suffix in _FILE_SIDECAR_SUFFIXES
    )


#: The two halves of ownership, published for `library.manage`. Deletion has to
#: ask the same question a scan asks, and asking it with a second copy of the
#: rule is how the two come to disagree -- with an unlink on the losing side.
FILE_SIDECAR_SUFFIXES: Final[tuple[str, ...]] = _FILE_SIDECAR_SUFFIXES


def is_managed_directory(directory: Path) -> bool:
    """Whether ``directory`` carries a marker saying this app created it."""
    return _read_marker(directory) is not None


def has_file_sidecar(path: Path) -> bool:
    """Whether ``path`` has a sidecar proving this app downloaded it."""
    return _has_file_sidecar(path)


def wallpaper_directory_from_noctalia() -> Path | None:
    """Read `[wallpaper] directory` out of Noctalia's settings.

    Reusing Noctalia's own configured directory means the two agree about what
    the library is without the user configuring it twice.
    """
    try:
        with paths.noctalia_settings_path().open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
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


def _walk(root: Path, budget: list[int], skipped: list[str]) -> Iterable[Path]:
    """Yield candidate files under ``root``, depth-first and bounded."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            skipped.append(f"{directory}: {error.strerror or error}")
            continue
        for entry in entries:
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
                elif entry.is_file(follow_symlinks=False) or entry.is_file():
                    yield Path(entry.path)
            except OSError as error:
                skipped.append(f"{entry.path}: {error.strerror or error}")


def scan(
    roots: Sequence[Path] | None = None,
    records: Mapping[str, pairings.Pairing] | None = None,
) -> Library:
    """Build a `Library` from ``roots`` (or the default roots).

    ``records`` are the stored pairing customizations. Passed in rather than
    read here, because a scan should not have opinions about whose choices it
    is honouring -- and because pairing has to happen exactly once. Resolving
    a second time somewhere else would recompute the defaults from disk and
    overwrite whatever this pass decided.
    """
    resolved_roots = tuple(roots) if roots is not None else default_roots()

    budget = [MAX_ENTRIES_EXAMINED]
    skipped: list[str] = []
    items: list[MediaItem] = []
    seen: set[Path] = set()
    marker_cache: dict[Path, dict[str, object] | None] = {}

    for root in resolved_roots:
        if not root.is_dir():
            skipped.append(f"{root}: not a directory")
            continue
        for path in _walk(root, budget, skipped):
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
            except OSError:
                continue
            seen.add(path)

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
            if marker is not None and _has_file_sidecar(path):
                ownership = Ownership.MANAGED
                provider = _provider_of(marker)

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

    items.sort(key=lambda item: (item.path.parent.as_posix(), item.name.lower()))
    paired = pairings.apply(items, resolved_roots, records)
    return Library(roots=resolved_roots, items=paired, skipped=tuple(skipped))
