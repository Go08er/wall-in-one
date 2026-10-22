"""Fixed words the new interface shows. Pure constants, never from the backend."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: A wallpaper's kind, as a word and as an icon.
KIND_LABEL: Final[dict[str, str]] = {"still": "Image", "video": "Video", "scene": "Scene"}
KIND_ICON: Final[dict[str, str]] = {
    "still": "image-x-generic-symbolic",
    "video": "video-x-generic-symbolic",
    "scene": "applications-games-symbolic",
}


def kind_text(kind: str, detail: str = "") -> str:
    """A wallpaper's kind as a word, with a Wallpaper Engine item's own type after it.

    "Scene \u00b7 web": every Wallpaper Engine item that isn't a video is
    listed as a scene (docs/wallpaper-engine.md), and says what it really is.
    """
    text = KIND_LABEL.get(kind, kind.capitalize())
    return f"{text} \u00b7 {detail}" if detail else text


#: How many titles the notice about unsupported Wallpaper Engine items names.
UNSUPPORTED_NAMED: Final = 10


def unsupported_notice(count: int) -> str:
    """What the Scenes view says about ``count`` items no renderer here runs; empty at 0.

    Both interfaces say it (`wall_in_one.library.workshop.unsupported_titles`).
    """
    if count <= 0:
        return ""
    if count == 1:
        return (
            "1 Wallpaper Engine wallpaper needs a runtime linux-wallpaperengine "
            "doesn\u2019t support yet"
        )
    return (
        f"{count} Wallpaper Engine wallpapers need a runtime linux-wallpaperengine "
        "doesn\u2019t support yet"
    )


def unsupported_details(titles: Sequence[str]) -> str:
    """The notice's tooltip: which they are, the first `UNSUPPORTED_NAMED` by name."""
    if not titles:
        return ""
    named = ", ".join(titles[:UNSUPPORTED_NAMED])
    more = len(titles) - UNSUPPORTED_NAMED
    listed = f"{named} and {more} more" if more > 0 else named
    return f"Listed, never played: {listed}. Executable wallpapers are never run, for security."


#: Said where a page or an action of the new interface is not ported yet.
CLASSIC_HINT: Final = (
    "To use it, choose the classic interface in Settings and restart Wall-in-One, "
    "or start it with --ui=classic."
)


def quoted(text: str) -> str:
    """``text`` in typographic double quotes, as the interface names things."""
    return f"\u201c{text}\u201d"
