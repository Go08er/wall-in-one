"""Dummy domain data for the UI prototype. Nothing here reads or writes real state.

The shapes mirror the real app closely enough to design against: wallpapers
are pairings (still + optional motion + color policy), playlists are ordered
entries that may repeat a wallpaper, schedule rules are "last match wins", and
displays may share one rotation or run their own.
"""

from __future__ import annotations

import colorsys
import hashlib

from . import art
from .models import Display, Palette, Playlist, RememberedDisplay, Rule, StoreItem, Wallpaper

# ---------------------------------------------------------------------------
# Wallpapers
# ---------------------------------------------------------------------------


def _w(id_, name, kind, style, seed, night, source="Local", **kw) -> Wallpaper:
    folders = {
        "Local": "~/Pictures/Wallpapers",
        "Wallhaven": "~/Pictures/Wallpapers/Wall-in-One/Downloads/Wallhaven",
        "MotionBGS": "~/Pictures/Wallpapers/Wall-in-One/Downloads/MotionBGS",
        "Workshop": "Steam Workshop · Wallpaper Engine",
    }
    defaults = {
        "still": ("3840 × 2160", "6.2 MB"),
        "video": ("2560 × 1440", "38 MB"),
        "scene": ("Scene", "112 MB"),
    }
    resolution, size = defaults[kind]
    still_note = {
        "still": "This image is its own still",
        "video": "Captured from the video at 0:03",
        "scene": "Captured from the scene preview",
    }[kind]
    return Wallpaper(
        id=id_,
        name=name,
        kind=kind,
        style=style,
        seed=seed,
        night=night,
        source=source,
        folder=kw.pop("folder", folders[source]),
        resolution=kw.pop("resolution", resolution),
        size=kw.pop("size", size),
        added=kw.pop("added", "12 Sep"),
        duration=kw.pop("duration", "0:24" if kind == "video" else ""),
        still_note=kw.pop("still_note", still_note),
        **kw,
    )


WALLPAPERS: list[Wallpaper] = [
    _w(
        "lily-pond",
        "Lily pond",
        "video",
        "pond",
        3,
        False,
        "MotionBGS",
        favorite=True,
        added="Today",
        tags=("frog", "water", "calm"),
    ),
    _w(
        "frog-dusk",
        "Frog at dusk",
        "video",
        "pond",
        11,
        True,
        "MotionBGS",
        favorite=True,
        added="Yesterday",
        duration="0:31",
    ),
    _w("moon-pond", "Moonlit lily pond", "scene", "pond", 27, True, "Workshop", favorite=True),
    _w("rain-window", "Rainy window", "video", "rain", 5, False, duration="1:02", color_mode="adaptive", scheme="soft"),
    _w(
        "neon-rain",
        "Neon rain",
        "scene",
        "rain",
        18,
        True,
        "Workshop",
        problem="linux-wallpaperengine stopped on HDMI-A-1 (exit status 1) on 21 Sep. "
        "It is skipped until you retry it.",
    ),
    _w(
        "alpine",
        "Alpine morning",
        "still",
        "peaks",
        2,
        False,
        "Wallhaven",
        favorite=True,
        resolution="5120 × 2880",
        size="9.8 MB",
    ),
    _w(
        "snowy-village",
        "Snowy village",
        "still",
        "peaks",
        41,
        False,
        still_note="Uses snowy-village-still.png beside the video",
    ),
    _w("midnight-ridges", "Midnight ridges", "still", "peaks", 9, True, "Wallhaven"),
    _w("golden-coast", "Golden coast", "video", "ocean", 4, False, "MotionBGS", duration="0:45"),
    _w("moon-bay", "Moon over the bay", "video", "ocean", 14, True, "MotionBGS", favorite=True),
    _w(
        "city-dusk",
        "City at dusk",
        "still",
        "city",
        6,
        False,
        "Wallhaven",
        color_mode="palette",
        palette="Tokyo Night",
    ),
    _w("night-skyline", "Night skyline", "video", "city", 21, True, duration="0:18"),
    _w("misty-pines", "Misty pines", "still", "forest", 8, False, "Wallhaven"),
    _w("forest-night", "Forest at night", "video", "forest", 30, True),
    _w("northern-lights", "Northern lights", "scene", "aurora", 12, True, "Workshop", favorite=True),
    _w("aurora-drift", "Aurora drift", "video", "aurora", 33, True, "MotionBGS"),
    _w("blocky-sunrise", "Blocky sunrise", "scene", "blocks", 7, False, "Workshop"),
    _w("overworld-noon", "Overworld noon", "still", "blocks", 15, False),
    _w("blocky-night", "Blocky night", "video", "blocks", 22, True),
    _w("cave-glow", "Cave glow", "still", "blocks", 35, True),
    _w("dune-sea", "Dune sea", "still", "dunes", 10, False, "Wallhaven", color_mode="keep", theme_mode="keep"),
    _w("desert-stars", "Desert stars", "still", "dunes", 24, True),
    _w("teal-bloom", "Teal bloom", "still", "abstract", 16, False, color_mode="adaptive", scheme="m3-rainbow"),
    _w("soft-gradient", "Soft gradient", "still", "abstract", 31, False),
    _w("deep-blur", "Deep blur", "still", "abstract", 40, True),
    _w("harbor-lights", "Harbor lights", "video", "ocean", 37, True, duration="0:52"),
    _w("rainy-street", "Rainy street", "video", "rain", 44, True, "MotionBGS"),
    _w("pine-ridge", "Pine ridge", "still", "forest", 47, False),
    _w("summit-glow", "Summit glow", "still", "peaks", 52, False, "Wallhaven"),
    _w("pond-ripples", "Pond ripples", "video", "pond", 55, False, duration="0:15"),
]

BY_ID = {wallpaper.id: wallpaper for wallpaper in WALLPAPERS}

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

SCHEMES: list[tuple[str, str, str]] = [
    ("m3-tonal-spot", "Tonal spot", "Calm, built around the main color"),
    ("m3-content", "Content", "Stays close to the picture"),
    ("m3-fruit-salad", "Fruit salad", "Playful, shifted hues"),
    ("m3-rainbow", "Rainbow", "Bold accents in several hues"),
    ("m3-monochrome", "Monochrome", "Grays only"),
    ("vibrant", "Vibrant", "Saturated and bright"),
    ("faithful", "Faithful", "The picture's own colors"),
    ("soft", "Soft", "Pastel and gentle"),
    ("dysfunctional", "Dysfunctional", "Deliberately clashing"),
    ("muted", "Muted", "Low saturation"),
]
SCHEME_NAME = {key: name for key, name, _ in SCHEMES}
DEFAULT_SCHEME = "m3-tonal-spot"

# Approximate colors of Noctalia's built-in palettes, for previews only.
PALETTES: dict[str, list[str]] = {
    "Ayu": ["#0b0e14", "#e6b450", "#59c2ff", "#aad94c", "#f07178"],
    "Catppuccin": ["#1e1e2e", "#cba6f7", "#89b4fa", "#a6e3a1", "#f38ba8"],
    "Dracula": ["#282a36", "#bd93f9", "#ff79c6", "#50fa7b", "#8be9fd"],
    "Eldritch": ["#212337", "#37f499", "#04d1f9", "#a48cf2", "#f265b5"],
    "Gruvbox": ["#282828", "#fabd2f", "#fb4934", "#b8bb26", "#83a598"],
    "Kanagawa": ["#1f1f28", "#7e9cd8", "#957fb8", "#98bb6c", "#e46876"],
    "Noctalia": ["#0f1117", "#a9aefe", "#9bfece", "#fff59b", "#fd4663"],
    "Nord": ["#2e3440", "#88c0d0", "#81a1c1", "#a3be8c", "#bf616a"],
    "Rosé Pine": ["#191724", "#ebbcba", "#c4a7e7", "#31748f", "#f6c177"],
    "Tokyo Night": ["#1a1b26", "#7aa2f7", "#bb9af7", "#9ece6a", "#f7768e"],
}
COMMUNITY_PALETTES = ["Everforest", "Solarized", "One Dark", "Material Ocean", "Oxocarbon"]


def _hex(h: float, l: float, s: float) -> str:  # noqa: E741
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0, min(1, l)), max(0, min(1, s)))
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def scheme_swatches(wallpaper: Wallpaper, scheme: str | None, dark: bool = True) -> list[str]:
    """Five plausible colors [surface, primary, secondary, tertiary, error].

    The real app asks `noctalia theme <image> --scheme <s>` for exact colors.
    These are stand-ins with the right character for each scheme.
    """
    scheme = scheme or DEFAULT_SCHEME
    look = art.look_for(*wallpaper.key)
    h = look.hue
    surf_l, prim_l = (0.09, 0.72) if dark else (0.95, 0.38)
    table = {
        "m3-tonal-spot": [(h, surf_l, 0.12), (h, prim_l, 0.48), (h, prim_l, 0.18), (h + 0.17, prim_l, 0.30)],
        "m3-content": [(h, surf_l, 0.20), (h, prim_l, 0.62), (h + 0.03, prim_l, 0.35), (h + 0.1, prim_l, 0.45)],
        "m3-fruit-salad": [
            (h - 0.14, surf_l, 0.14),
            (h - 0.14, prim_l, 0.62),
            (h - 0.14, prim_l, 0.35),
            (h + 0.1, prim_l, 0.55),
        ],
        "m3-rainbow": [(h, surf_l, 0.05), (h, prim_l, 0.70), (h + 0.33, prim_l, 0.60), (h + 0.66, prim_l, 0.60)],
        "m3-monochrome": [
            (h, surf_l, 0.0),
            (h, prim_l, 0.0),
            (h, (prim_l + surf_l) / 2 + 0.1, 0.0),
            (h, prim_l - 0.15, 0.0),
        ],
        "vibrant": [
            (h, surf_l, 0.25),
            (h, prim_l - 0.08, 0.95),
            (h + 0.08, prim_l - 0.05, 0.85),
            (h + 0.5, prim_l - 0.05, 0.85),
        ],
        "faithful": [
            (h, surf_l + 0.03, 0.30),
            (h, prim_l - 0.1, 0.55),
            (h - 0.04, prim_l - 0.05, 0.45),
            (0.11, prim_l, 0.75),
        ],
        "soft": [
            (h, surf_l + (0.03 if dark else -0.01), 0.10),
            (h, min(0.85, prim_l + 0.1), 0.40),
            (h + 0.06, min(0.85, prim_l + 0.12), 0.30),
            (h + 0.2, min(0.86, prim_l + 0.12), 0.32),
        ],
        "dysfunctional": [
            (h + 0.5, surf_l, 0.35),
            (h + 0.25, prim_l, 0.90),
            (h + 0.75, prim_l, 0.85),
            (h, prim_l, 0.90),
        ],
        "muted": [
            (h, surf_l, 0.06),
            (h, prim_l - 0.05, 0.18),
            (h + 0.05, prim_l - 0.05, 0.12),
            (h + 0.15, prim_l - 0.05, 0.14),
        ],
    }[scheme]
    return [_hex(*spec) for spec in table] + [_hex(0.99, 0.62 if dark else 0.45, 0.75)]


def hls_of(hex_color: str) -> tuple[float, float, float]:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)


def accent_from(swatches: list[str], dark: bool = True) -> tuple[str, str] | None:
    """(accent background, accent text) from a palette's primary color.

    The background stays mid-tone so white button text remains readable; the
    text variant is lighter on dark surfaces and darker on light ones.
    """
    if len(swatches) < 2:
        return None
    h, _l, s = hls_of(swatches[1])
    s = s if s < 0.05 else max(s, 0.40)
    return _hex(h, 0.42, s), _hex(h, 0.74 if dark else 0.36, s)


def surfaces_from(swatches: list[str], dark: bool = True) -> dict[str, str] | None:
    """libadwaita surface colors tinted by a palette, the way the real app maps
    Noctalia's surface tokens (window = surface, view = surface-container-low…).

    The palette's own surface sets the hue and tint; lightness follows
    libadwaita's steps so contrast stays familiar. None = libadwaita defaults.
    """
    if not swatches:
        return None
    h, l, s = hls_of(swatches[0])  # noqa: E741
    if dark:
        base = min(0.19, max(0.10, l))
        s = min(s, 0.35)
        steps = {
            "window": 0.0, "view": -0.02, "headerbar": 0.05, "sidebar": 0.05,
            "secondary-sidebar": 0.025, "card": 0.065, "dialog": 0.08, "popover": 0.08,
        }  # fmt: skip
    else:
        base = l if l >= 0.85 else 0.965
        s = min(0.45, s * 1.5)
        steps = {
            "window": 0.0, "view": 0.03, "headerbar": 0.03, "sidebar": -0.045,
            "secondary-sidebar": -0.02, "card": 0.03, "dialog": 0.02, "popover": 0.03,
        }  # fmt: skip
    return {name: _hex(h, base + step, s) for name, step in steps.items()}


def wallpaper_swatches(wallpaper: Wallpaper, dark: bool = True) -> list[str]:
    if wallpaper.color_mode == "palette" and wallpaper.palette in PALETTES:
        return PALETTES[wallpaper.palette]
    if wallpaper.color_mode == "keep":
        return []
    return scheme_swatches(wallpaper, wallpaper.scheme, dark)


# Noctalia palettes: full light and dark key sets, derived from five colors.


def _on(color: str) -> str:
    """A readable foreground for a fill: near-black or near-white in the same hue."""
    hue, lightness, saturation = hls_of(color)
    if lightness > 0.55:
        return _hex(hue, 0.12, min(saturation, 0.5))
    return _hex(hue, 0.96, min(saturation, 0.3))


def derive(five: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Full light and dark key sets from [surface, primary, secondary, tertiary, error]."""
    surface, primary, secondary, tertiary, error = five
    sh, _sl, ss = hls_of(surface)
    dark = {"surface": surface, "primary": primary, "secondary": secondary, "tertiary": tertiary, "error": error}
    dark["surface_variant"] = _hex(sh, hls_of(surface)[1] + 0.08, ss)
    dark["on_surface"] = _hex(sh, 0.90, min(ss, 0.18))
    dark["on_surface_variant"] = _hex(sh, 0.72, min(ss, 0.14))
    dark["outline"] = _hex(sh, 0.42, min(ss, 0.12))
    dark["shadow"] = "#000000"
    light: dict[str, str] = {}
    for key, value in (("primary", primary), ("secondary", secondary), ("tertiary", tertiary), ("error", error)):
        h, _l, s = hls_of(value)
        light[key] = _hex(h, 0.40, max(s, 0.35))
    ph = hls_of(primary)[0]
    light["surface"] = _hex(ph, 0.97, 0.30)
    light["surface_variant"] = _hex(ph, 0.90, 0.22)
    light["on_surface"] = _hex(ph, 0.12, 0.15)
    light["on_surface_variant"] = _hex(ph, 0.32, 0.12)
    light["outline"] = _hex(ph, 0.55, 0.08)
    light["shadow"] = "#000000"
    for variant in (dark, light):
        for key in ("primary", "secondary", "tertiary", "error"):
            variant[f"on_{key}"] = _on(variant[key])
    return light, dark


def _community_colors(name: str) -> list[str]:
    seed = int(hashlib.sha1(name.encode()).hexdigest()[:8], 16)
    hue = (seed % 360) / 360
    return [
        _hex(hue, 0.11, 0.18),
        _hex(hue, 0.72, 0.55),
        _hex(hue + 0.12, 0.70, 0.45),
        _hex(hue + 0.45, 0.72, 0.50),
        _hex(0.99, 0.66, 0.70),
    ]


def make_palette(name: str, origin: str, five: list[str]) -> Palette:
    light, dark = derive(five)
    return Palette(name, origin, light, dark)


def make_palettes() -> list[Palette]:
    """Every palette the demo knows: one of yours, the built-ins, the community catalog."""
    palettes = [make_palette("Lily pad", "custom", ["#10201a", "#7fd6a4", "#a8c8b4", "#f2b5d4", "#ffb4ab"])]
    palettes += [make_palette(name, "builtin", colors) for name, colors in PALETTES.items()]
    palettes += [make_palette(name, "community", _community_colors(name)) for name in COMMUNITY_PALETTES]
    return palettes


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


def cover_keys(playlist: Playlist) -> tuple[tuple[str, int, bool], ...]:
    """The pictures of a playlist's cover: its first four different wallpapers."""
    seen: list[tuple[str, int, bool]] = []
    for entry in playlist.entries:
        key = BY_ID[entry].key
        if key not in seen:
            seen.append(key)
        if len(seen) == 4:
            break
    return tuple(seen)


PLAYLISTS: list[Playlist] = [
    Playlist(
        "frog-day",
        "Frog day",
        [
            "lily-pond",
            "pond-ripples",
            "rain-window",
            "golden-coast",
            "misty-pines",
            "lily-pond",
            "alpine",
            "teal-bloom",
        ],
        interval=30,
    ),
    Playlist(
        "frog-night",
        "Frog night",
        ["frog-dusk", "moon-pond", "neon-rain", "forest-night", "moon-bay", "rainy-street"],
        interval=60,
        shuffle=True,
    ),
    Playlist("mc-day", "MC day", ["blocky-sunrise", "overworld-noon", "alpine", "summit-glow"], interval=15),
    Playlist("mc-night", "MC night", ["blocky-night", "cave-glow", "northern-lights", "aurora-drift"], interval=60),
    Playlist(
        "cozy-rain",
        "Cozy rain",
        ["rain-window", "rainy-street", "neon-rain", "harbor-lights"],
        interval=120,
        shuffle=True,
    ),
    Playlist(
        "all-media",
        "All wallpapers",
        [w.id for w in WALLPAPERS],
        interval=60,
        shuffle=True,
        automatic="Every wallpaper in your library, updated automatically",
        icon="view-grid-symbolic",
    ),
]
PLAYLIST_BY_ID = {playlist.id: playlist for playlist in PLAYLISTS}

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

# Order matters: a later rule wins where rules overlap (the real semantics).
RULES: list[Rule] = [
    Rule("daytime", "frog-day", start="07:00", end="18:00"),
    Rule("evening", "frog-night", start="18:00", end="07:00"),
    Rule("weekend-days", "mc-day", days=[5, 6], start="09:00", end="19:00"),
    Rule("weekend-nights", "mc-night", days=[4, 5], start="22:00", end="02:00"),
    Rule("december", "cozy-rain", months=[11]),
    Rule(
        "work-screen", "cozy-rain", days=[0, 1, 2, 3, 4], start="09:00", end="17:00", display="HDMI-A-1", enabled=False
    ),
]
FALLBACK_PLAYLIST = "all-media"

# ---------------------------------------------------------------------------
# Displays
# ---------------------------------------------------------------------------


DISPLAYS: list[Display] = [
    Display("DP-1", "Dell U2723QE", "3840 × 2160 @ 60 Hz", 1.5, 0, 0, 2560, 1440, primary=True),
    Display("HDMI-A-1", "LG 24GL600F", "1920 × 1080 @ 144 Hz", 1.0, 2560, 200, 1920, 1080),
]

# Outputs the runtime still keeps routes for although they are unplugged.
REMEMBERED_DISPLAYS: list[RememberedDisplay] = [
    RememberedDisplay("eDP-1", "Laptop screen", "computer-symbolic", "2 days ago", "mc-night"),
    RememberedDisplay("DP-2", "Samsung Odyssey G7", "video-display-symbolic", "3 weeks ago", ""),
]

# Per-display renderer settings (Displays → Advanced) until a display changes them.
DISPLAY_SETTINGS_DEFAULT = {"fps": 0, "sound": False, "scaling": "fill", "covered": True}

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _store(provider: str, count: int, start: int) -> list[StoreItem]:
    names = [
        ("Quiet summit", "peaks", False),
        ("Harbor at night", "ocean", True),
        ("Lantern street", "city", True),
        ("Fern hollow", "forest", False),
        ("Polar sky", "aurora", True),
        ("Frog chorus", "pond", True),
        ("Pixel plains", "blocks", False),
        ("Window rain", "rain", False),
        ("Amber dunes", "dunes", False),
        ("Glass bloom", "abstract", False),
        ("Cold front", "peaks", True),
        ("Sunset pier", "ocean", False),
        ("Neon district", "city", True),
        ("Old growth", "forest", False),
        ("Green veil", "aurora", True),
        ("Lotus morning", "pond", False),
        ("Night biome", "blocks", True),
        ("Storm glass", "rain", True),
    ]
    items = []
    for index in range(count):
        title, style, night = names[(index + start) % len(names)]
        moving = provider == "MotionBGS"
        items.append(
            StoreItem(
                id=f"{provider.lower()}-{index}",
                title=title,
                provider=provider,
                style=style,
                seed=100 + start * 3 + index * 7,
                night=night,
                resolution="2560 × 1440" if moving else ("3840 × 2160" if index % 3 else "5120 × 2880"),
                detail=f"0:{18 + index % 40:02d} · loop" if moving else f"{(index * 37) % 900 + 40} favorites",
                tags=(style, "night" if night else "day", "4k" if not moving else "live"),
                in_library=index in (2, 7),
            )
        )
    return items


STORE: dict[str, list[StoreItem]] = {
    "Wallhaven": _store("Wallhaven", 18, 0),
    "MotionBGS": _store("MotionBGS", 15, 4),
}

# ---------------------------------------------------------------------------
# Settings / diagnostics
# ---------------------------------------------------------------------------

LIBRARY_FOLDERS = [
    ("~/Pictures/Wallpapers", "1,204 files · Downloads and captured stills are saved here", True),
    ("~/Videos/Live wallpapers", "86 videos", False),
    ("/mnt/archive/wallpapers", "Not found — drive may be unplugged", False),
]

RUNTIME_LOG = [
    "09:00:01  schedule  Following “Daytime” → Frog day on all displays",
    "09:30:00  rotation  Next wallpaper: Rainy window (DP-1, HDMI-A-1)",
    "09:30:02  colors   Applied adaptive scheme Soft (dark)",
    "10:00:00  rotation  Next wallpaper: Golden coast",
    "10:12:47  power     On battery — animations stopped, stills kept",
    "10:41:03  power     AC power restored — animations resumed",
    "11:02:15  renderer  linux-wallpaperengine exited (status 1) for Neon rain on HDMI-A-1",
    "11:02:15  renderer  Neon rain skipped for this session; showing its still",
    "11:30:00  rotation  Next wallpaper: Misty pines",
]

# Lines with file paths, so "Hide file paths" has something to hide. Shown
# after RUNTIME_LOG and sorted in with it by time.
RUNTIME_LOG_EXTRA = [
    "09:00:00  library   Scanned /home/goober/Pictures/Wallpapers — 1,204 files",
    "09:00:00  library   Skipped /mnt/archive/wallpapers — folder not found",
    "11:02:15  renderer  Scene output saved to /home/goober/.local/state/wall-in-one/scenes/neon-rain.log",
]

# Settings rows that only the Settings page shows.
PREFERENCES = {
    "workshop": True,
    "shuffle": False,
    "autostart": True,
    "covered": "pause",
    "theme_mode": "auto",
    "purity": "sketchy",
    "decoding": "auto",
    "smoothing": "off",
    "sound": False,
    "volume": 100,
    "run_scenes": True,
    "fps": "30",
    "scaling": "",
    "edges": "",
    "hide_paths": True,
}
