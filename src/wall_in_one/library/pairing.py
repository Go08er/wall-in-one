"""Finding the still that stands behind a video, by convention.

The read half, and the narrow one: given a video, which still already
represents it. `library.pairings` (plural) owns the record, the choice and the
file, and calls in here for the default. `library.stills` is the write half.

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
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from wall_in_one.library.model import IMAGE_EXTENSIONS

#: Written by us next to a video to record which still represents it.
SIDECAR_SUFFIX: Final = ".wall-in-one.json"

#: Everything this app writes into someone's wallpaper root goes under here,
#: so that one directory can be deleted to undo all of it. `providers.download`
#: names the same directory for its own downloads.
MANAGED_PARENT: Final = "Wall-in-One"

#: Directory generated stills are written into, beneath `MANAGED_PARENT`.
AUTOMATIC_STILLS_DIRECTORY: Final = "Automatic Stills"

#: The key a sidecar records the still under.
SIDECAR_STILL_KEY: Final = "still_path"

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
    recorded = document.get(SIDECAR_STILL_KEY)
    if not isinstance(recorded, str) or not recorded:
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = video.parent / candidate
    return candidate if candidate.is_file() else None


def still_directory(root: Path) -> Path:
    """Where generated stills live under ``root``. Shared with `library.stills`,
    so the half that writes them and the half that finds them cannot drift."""
    return root / MANAGED_PARENT / AUTOMATIC_STILLS_DIRECTORY


def _automatic_still(video: Path, roots: Iterable[Path]) -> Path | None:
    """Look for a generated still under a managed `Automatic Stills` directory."""
    for root in roots:
        directory = still_directory(root)
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
