"""View-model types: what pages read from ``AppState``.

Plain records with no lookups and no drawing, so a real-app adapter can build
the same shapes from the library, the stores and the runtime's status. Pages
may read these freely; they change them only through ``AppState`` methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: (style, seed, night): what the procedural art draws a picture from.
ArtKey = tuple[str, int, bool]


@dataclass
class Wallpaper:
    id: str
    name: str
    kind: str  # still | video | scene
    style: str
    seed: int
    night: bool
    source: str  # Local | Wallhaven | MotionBGS | Workshop
    folder: str
    resolution: str
    size: str
    added: str
    duration: str = ""
    favorite: bool = False
    # Color policy: "adaptive" (generated from the still), "palette" (a named
    # Noctalia palette) or "keep" (leave the desktop colors alone).
    color_mode: str = "adaptive"
    scheme: str | None = None  # None = app default
    palette: str | None = None
    theme_mode: str = "auto"  # auto | dark | light | keep
    still_note: str = ""
    problem: str = ""  # a renderer failure the runtime reported, if any
    tags: tuple[str, ...] = ()

    @property
    def key(self) -> ArtKey:
        return (self.style, self.seed, self.night)

    @property
    def is_moving(self) -> bool:
        return self.kind != "still"


@dataclass
class Playlist:
    id: str
    name: str
    entries: list[str]
    interval: int = 30
    shuffle: bool = False
    automatic: str = ""  # "" for user playlists; otherwise why it is automatic
    icon: str = ""


@dataclass
class Rule:
    id: str
    playlist: str
    days: list[int] = field(default_factory=list)  # empty = every day
    start: str | None = None  # None/None = all day
    end: str | None = None
    months: list[int] = field(default_factory=list)  # empty = every month
    display: str = ""  # "" = all displays
    enabled: bool = True


@dataclass
class Display:
    connector: str
    model: str
    mode: str
    scale: float
    x: int
    y: int
    width: int  # logical
    height: int
    primary: bool = False


@dataclass
class RememberedDisplay:
    """A display the runtime still keeps settings for although it is unplugged."""

    connector: str
    model: str
    icon: str
    seen: str
    playlist: str  # its own playlist, "" = the default


@dataclass
class StoreItem:
    id: str
    title: str
    provider: str
    style: str
    seed: int
    night: bool
    resolution: str
    detail: str
    tags: tuple[str, ...]
    in_library: bool = False

    @property
    def key(self) -> ArtKey:
        return (self.style, self.seed, self.night)


@dataclass
class Folder:
    """A library folder. The first one is where downloads and stills are saved."""

    path: str
    count: str = ""
    missing: bool = False
    scanning: bool = False


@dataclass
class Palette:
    """A Noctalia palette: its light and dark key sets."""

    name: str
    origin: str  # custom | builtin | community
    light: dict[str, str] = field(default_factory=dict)
    dark: dict[str, str] = field(default_factory=dict)

    def strip(self, dark: bool) -> list[str]:
        values = self.dark if dark else self.light
        return [values[key] for key in ("surface", "primary", "secondary", "tertiary", "error")]

    @property
    def editable(self) -> bool:
        return self.origin == "custom"
