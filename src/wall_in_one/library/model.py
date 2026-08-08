"""What a wallpaper is, as far as this app is concerned."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Self

#: Extensions we will hand to Noctalia as a still wallpaper.
IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}
)

#: Extensions we will hand to mpvpaper as a video wallpaper.
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".gif"}
)

#: `.gif` is in both sets on purpose -- it is a still to Noctalia and an
#: animation to mpvpaper. Which one it counts as is decided per file by
#: `classify`, not by the extension alone.
AMBIGUOUS_EXTENSIONS: Final[frozenset[str]] = IMAGE_EXTENSIONS & VIDEO_EXTENSIONS


class Kind(Enum):
    """How a wallpaper is shown.

    `SCENE` is the odd one and is why this is not a two-way split: a Wallpaper
    Engine scene is not a file at all. Its content is packed inside a
    `scene.pkg` that only `linux-wallpaperengine` can read, so it is named by
    its Steam Workshop id and its `path` points at the directory it lives in
    rather than at anything playable.
    """

    STILL = "still"
    VIDEO = "video"
    SCENE = "scene"

    @property
    def moves(self) -> bool:
        """Whether showing this needs a renderer above the wallpaper.

        The distinction the dynamics setting is really about. A scene and a
        video differ in which renderer, not in whether one is needed.
        """
        return self is not Kind.STILL


class Ownership(Enum):
    """Who put the file there, and therefore who may delete it."""

    #: The user's own file. We never delete these.
    USER = "user"
    #: Downloaded into a directory we manage, with a sidecar proving it.
    MANAGED = "managed"


def classify(path: Path) -> Kind | None:
    """Decide how to play ``path``, or ``None`` if we cannot play it.

    A `.gif` is treated as a video: mpvpaper animates it, and a still-image
    wallpaper of an animated gif shows only the first frame, which is not what
    anyone means by setting a gif as their wallpaper.
    """
    extension = path.suffix.lower()
    if extension in AMBIGUOUS_EXTENSIONS:
        return Kind.VIDEO
    if extension in VIDEO_EXTENSIONS:
        return Kind.VIDEO
    if extension in IMAGE_EXTENSIONS:
        return Kind.STILL
    return None


@dataclass(frozen=True, slots=True)
class MediaItem:
    """One wallpaper on disk."""

    path: Path
    kind: Kind
    size: int
    mtime: int
    ownership: Ownership = Ownership.USER
    provider: str = "local"
    #: A Wallpaper Engine Workshop id, when `kind` is `SCENE`. Empty otherwise.
    #: The renderer is given this rather than a path, because there is no file
    #: to give it.
    scene: str = ""
    #: What to call this where a person will read it. Empty for anything whose
    #: filename already says what it is, which is nearly everything; a Workshop
    #: scene needs it because its directory is named `1647046763` and its
    #: wallpaper is called "Toothless in a Field".
    title: str = ""
    #: For a video: a still to show when dynamics are paused. See
    #: `wall_in_one.library.pairing`.
    paired_still: Path | None = None
    #: A read-only source suitable for representing media that has no file of
    #: its own. Workshop scenes use the author-supplied preview as a fallback
    #: until their generated still exists.
    preview: Path | None = None

    @property
    def name(self) -> str:
        """What to show, and what the search box matches against."""
        return self.title or self.path.stem

    @property
    def is_video(self) -> bool:
        return self.kind is Kind.VIDEO

    @property
    def is_moving(self) -> bool:
        """Whether this needs a renderer: a video or a scene."""
        return self.kind.moves

    @property
    def deletable(self) -> bool:
        """Only files we downloaded into a directory we manage."""
        return self.ownership is Ownership.MANAGED

    def playback_path(self, *, dynamics_enabled: bool) -> Path | None:
        """The file to actually display, or ``None`` if it cannot be shown now.

        With dynamics off, a video falls back to its paired still. A video with
        no paired still has nothing to show, which is what ``None`` means --
        the playlist skips it rather than silently starting the video anyway.
        """
        if not self.kind.moves or dynamics_enabled:
            return self.path
        return self.paired_still

    def with_still(self, still: Path | None) -> Self:
        from dataclasses import replace

        return replace(self, paired_still=still)


@dataclass(frozen=True, slots=True)
class Library:
    """Everything found under the configured roots."""

    roots: tuple[Path, ...]
    items: tuple[MediaItem, ...]
    #: Directories that were skipped: unreadable, or past the scan ceiling.
    skipped: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.items)

    def playable(self, *, dynamics_enabled: bool) -> tuple[MediaItem, ...]:
        """Items that can be shown under the current dynamics setting."""
        return tuple(
            item for item in self.items if item.playback_path(dynamics_enabled=dynamics_enabled)
        )

    def find(self, path: Path) -> MediaItem | None:
        for item in self.items:
            if item.path == path:
                return item
        return None

    @property
    def videos(self) -> tuple[MediaItem, ...]:
        return tuple(item for item in self.items if item.kind is Kind.VIDEO)

    @property
    def scenes(self) -> tuple[MediaItem, ...]:
        return tuple(item for item in self.items if item.kind is Kind.SCENE)

    @property
    def stills(self) -> tuple[MediaItem, ...]:
        return tuple(item for item in self.items if item.kind is Kind.STILL)
