"""Pairing a video wallpaper with a still.

When dynamics are paused the app shows a still instead of the video, so every
video wants a still standing behind it. There are three ways one gets there,
tried in this order:

1. a sidecar we wrote, `<video>.wall-in-one.json`, naming the still outright;
2. a file of the same name in the managed `Automatic Stills` directory, which
   is where generated stills land;
3. a sibling named by convention -- `foo.mp4` pairs with `foo-still.png` or
   plain `foo.png`.

Rule 3 is what the user's own library already does (`snowy-village-still.png`),
so it is not a fallback so much as the common case.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from wall_in_one.library.model import IMAGE_EXTENSIONS, Kind, MediaItem

#: Written by us next to a video to record which still represents it.
SIDECAR_SUFFIX: Final = ".wall-in-one.json"

#: Directory generated stills are written into, relative to a managed root.
AUTOMATIC_STILLS_DIRECTORY: Final = "Automatic Stills"

#: Suffix on a hand-made still, before the extension.
STILL_NAME_SUFFIX: Final = "-still"

#: A sidecar is a few hundred bytes. Anything larger is not one of ours.
MAX_SIDECAR_BYTES: Final = 64 * 1024

#: Preference order when several stills could serve. Lossless first, since a
#: still is usually a frame grab.
_STILL_EXTENSION_ORDER: Final[tuple[str, ...]] = (".png", ".webp", ".avif", ".jpg", ".jpeg")


def _still_extensions() -> tuple[str, ...]:
    """Image extensions, preferred ones first, and never `.gif`.

    A gif pairing a video would itself be played as a video, which defeats the
    point of pausing dynamics.
    """
    preferred = [ext for ext in _STILL_EXTENSION_ORDER if ext in IMAGE_EXTENSIONS]
    rest = sorted(IMAGE_EXTENSIONS - set(preferred) - {".gif"})
    return tuple(preferred + rest)


def read_sidecar(video: Path) -> Path | None:
    """Read `<video>.wall-in-one.json` and return the still it names."""
    sidecar = video.with_name(video.name + SIDECAR_SUFFIX)
    try:
        if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        raw = sidecar.read_bytes()
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    recorded = document.get("still_path")
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = video.parent / candidate
    return candidate if candidate.is_file() else None


def _automatic_still(video: Path, roots: Iterable[Path]) -> Path | None:
    """Look for a generated still under a managed `Automatic Stills` directory."""
    for root in roots:
        directory = root / "Wall-in-One" / AUTOMATIC_STILLS_DIRECTORY
        for extension in _still_extensions():
            candidate = directory / (video.stem + extension)
            if candidate.is_file():
                return candidate
    return None


def _sibling_still(video: Path) -> Path | None:
    """`foo.mp4` -> `foo-still.png`, else `foo.png`."""
    for stem in (video.stem + STILL_NAME_SUFFIX, video.stem):
        for extension in _still_extensions():
            candidate = video.with_name(stem + extension)
            if candidate != video and candidate.is_file():
                return candidate
    return None


def find_still(video: Path, roots: Iterable[Path] = ()) -> Path | None:
    """Best still for ``video``, or ``None`` if it has none."""
    roots = tuple(roots)
    from_sidecar = read_sidecar(video)
    if from_sidecar is not None:
        return from_sidecar
    generated = _automatic_still(video, roots)
    if generated is not None:
        return generated
    return _sibling_still(video)


def apply(
    items: Iterable[MediaItem],
    roots: Iterable[Path] = (),
    *,
    resolver: Mapping[Path, Path] | None = None,
) -> tuple[MediaItem, ...]:
    """Attach stills to videos and drop stills that exist only as a pair.

    A still whose whole job is representing a video is not a separate
    wallpaper, so it is removed from the returned items -- otherwise the same
    picture turns up twice in the rotation, once on its own and once as the
    paused form of the video.

    A still nobody pairs with stays, which is why the user's three root-level
    `*-still.png` files survive: they have no video behind them.

    ``resolver`` short-circuits disk lookups, for tests and for reusing a scan.
    """
    materialised = list(items)
    roots = tuple(roots)

    paired: dict[Path, Path] = {}
    for item in materialised:
        if item.kind is not Kind.VIDEO:
            continue
        still = (
            resolver.get(item.path) if resolver is not None else find_still(item.path, roots=roots)
        )
        if still is not None:
            paired[item.path] = still

    consumed = set(paired.values())
    result: list[MediaItem] = []
    for item in materialised:
        if item.kind is Kind.VIDEO:
            result.append(item.with_still(paired.get(item.path)))
        elif item.path not in consumed:
            result.append(item)
    return tuple(result)
