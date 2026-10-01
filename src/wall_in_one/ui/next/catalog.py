"""Fixed words the new interface shows. Pure constants, never from the backend."""

from __future__ import annotations

from typing import Final

#: A wallpaper's kind, as a word and as an icon.
KIND_LABEL: Final[dict[str, str]] = {"still": "Image", "video": "Video", "scene": "Scene"}
KIND_ICON: Final[dict[str, str]] = {
    "still": "image-x-generic-symbolic",
    "video": "video-x-generic-symbolic",
    "scene": "applications-games-symbolic",
}

#: Said where a page or an action of the new interface is not ported yet.
CLASSIC_HINT: Final = "Start Wall-in-One with --ui=classic to use it."


def quoted(text: str) -> str:
    """``text`` in typographic double quotes, as the interface names things."""
    return f"\u201c{text}\u201d"
