"""Dummy provider catalog for the Store page.

Everything is deterministic: the same search, sort and filters always return the
same results, so screenshots are reproducible. The options mirror what the real
providers offer (Wallhaven's API parameters, MotionBGS's browse modes).
"""

from __future__ import annotations

import colorsys
import random
from dataclasses import dataclass

from .. import art, data
from ..models import StoreItem

# ---------------------------------------------------------------------------
# Providers and their options
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    icon: str
    site: str
    noun: str  # what it offers, for placeholders and tooltips


PROVIDERS = {
    "Wallhaven": Provider("Wallhaven", "image-x-generic-symbolic", "wallhaven.cc", "images"),
    "MotionBGS": Provider("MotionBGS", "video-x-generic-symbolic", "motionbgs.com", "live wallpapers"),
}

# Wallhaven sorting (API ``sorting``); "relevance" only makes sense with a query.
SORTS = [
    ("relevance", "Relevance"),
    ("toplist", "Popular"),
    ("date_added", "Latest"),
    ("hot", "Hot"),
    ("views", "Most viewed"),
    ("favorites", "Most favorited"),
    ("random", "Random"),
]
SORT_LABEL = dict(SORTS)
# Wallhaven ``topRange``.
RANGES = [
    ("1d", "Today", "today"),
    ("3d", "Last 3 days", "in the last 3 days"),
    ("1w", "This week", "this week"),
    ("1M", "This month", "this month"),
    ("3M", "Last 3 months", "in the last 3 months"),
    ("6M", "Last 6 months", "in the last 6 months"),
    ("1y", "This year", "this year"),
]
# Wallhaven ``atleast``: (key, menu label, button label, minimum height).
SIZES = [
    ("any", "Any size", "Any size", 0),
    ("1080", "1080p or larger", "1080p+", 1080),
    ("1440", "1440p or larger", "1440p+", 1440),
    ("2160", "4K or larger", "4K+", 2160),
    ("2880", "5K or larger", "5K+", 2880),
]
# Wallhaven ``ratios``: (key, menu label, button label).
RATIOS = [
    ("any", "Any ratio", "Any ratio"),
    ("16:9", "16:9", "16:9"),
    ("16:10", "16:10", "16:10"),
    ("21:9", "21:9 ultrawide", "21:9"),
    ("32:9", "32:9 super ultrawide", "32:9"),
    ("portrait", "Portrait", "Portrait"),
]
CATEGORIES = ["General", "Anime", "People"]
PURITIES = ["SFW", "Sketchy", "NSFW"]
# Wallhaven's color search palette (``colors``), in the site's order.
COLORS = [
    "660000",
    "990000",
    "cc0000",
    "cc3333",
    "ea4c88",
    "993399",
    "663399",
    "333399",
    "0066cc",
    "0099cc",
    "66cccc",
    "77cc33",
    "669900",
    "336600",
    "666600",
    "999900",
    "cccc33",
    "ffff00",
    "ffcc33",
    "ff9900",
    "ff6600",
    "cc6633",
    "996633",
    "663300",
    "000000",
    "999999",
    "cccccc",
    "ffffff",
    "424153",
]
# MotionBGS browse modes: by genre and by quality ("Latest" is the order).
MOTION_CATEGORIES = [
    ("all", "All categories"),
    ("nature", "Nature"),
    ("anime", "Anime"),
    ("games", "Games"),
    ("space", "Space"),
    ("cyberpunk", "Cyberpunk"),
    ("fantasy", "Fantasy"),
    ("animals", "Animals"),
    ("abstract", "Abstract"),
]
MOTION_QUALITIES = [("any", "HD or 4K", "Any quality"), ("4k", "4K only", "4K")]

STYLE_TAGS = {
    "peaks": ("mountains", "landscape", "snow"),
    "ocean": ("sea", "coast", "water"),
    "city": ("city", "skyline", "street"),
    "forest": ("trees", "nature", "fog"),
    "aurora": ("aurora", "sky", "stars"),
    "pond": ("frog", "lily pad", "water"),
    "blocks": ("minecraft", "pixel art", "voxel"),
    "rain": ("rain", "window", "cozy"),
    "dunes": ("desert", "sand", "minimal"),
    "abstract": ("abstract", "gradient", "shapes"),
}

UPLOADERS = ["mizuame", "lunarfern", "k0ra", "pixelmoth", "tidewell", "ashgrove", "northpaw", "velvetgrid"]
ADDED = ["Today", "Yesterday", "2 days ago", "3 days ago", "5 days ago", "Last week", "2 weeks ago", "Last month"]

# Extra titles so "Load more" brings new pictures, not repeats (none clash with data.STORE).
STYLE_TITLES = {
    "peaks": [
        "Granite light",
        "High pass",
        "Blue hour ridge",
        "Alpenglow",
        "Glacier rim",
        "Summit haze",
        "Snowline",
        "Ridge walk",
        "Cloud sea",
    ],
    "ocean": [
        "Tidal glass",
        "Night ferry",
        "Kelp light",
        "Salt spray",
        "Low tide",
        "Harbor mist",
        "Moonlit cove",
        "Breakwater",
        "Open water",
    ],
    "city": [
        "Rooftop rain",
        "Paper lanterns",
        "Tram lines",
        "Crosswalk glow",
        "Alley neon",
        "Skyline haze",
        "Night market",
        "Overpass",
        "Window grid",
    ],
    "forest": [
        "Moss path",
        "Cedar fog",
        "Lantern grove",
        "Fern light",
        "Birch hollow",
        "Pine shadow",
        "Deep woods",
        "Canopy",
        "Morning mist",
    ],
    "aurora": [
        "Solar wind",
        "Magnetic veil",
        "Arctic halo",
        "Ribbon sky",
        "Polar night",
        "Green curtain",
        "Star drift",
        "Sky river",
        "Northern hush",
    ],
    "pond": [
        "Reed song",
        "Lily lanterns",
        "Toad hollow",
        "Pond glass",
        "Dragonfly noon",
        "Still water",
        "Lotus dusk",
        "Ripple field",
        "Frog parade",
    ],
    "blocks": [
        "Torch cavern",
        "Sky island",
        "Cherry village",
        "Cube sunrise",
        "Redstone dusk",
        "Pixel harbor",
        "Block valley",
        "Moonlit farm",
        "Ore vein",
    ],
    "rain": [
        "Drizzle cafe",
        "Fogged pane",
        "Monsoon street",
        "Wet asphalt",
        "Rain on glass",
        "Gray morning",
        "Puddle light",
        "Umbrella walk",
        "Storm window",
    ],
    "dunes": [
        "Salt flats",
        "Dune ripple",
        "Ochre wind",
        "Sand sea",
        "Desert noon",
        "Mirage",
        "Red canyon",
        "Dune shadow",
        "Star dunes",
    ],
    "abstract": [
        "Liquid chrome",
        "Soft prism",
        "Ink bloom",
        "Color field",
        "Velvet wave",
        "Glass gradient",
        "Paper fold",
        "Neon drift",
        "Silk noise",
    ],
}
_STYLE_ORDER = ["peaks", "city", "forest", "ocean", "abstract", "aurora", "rain", "blocks", "dunes", "pond"]

#: How many results a provider "has" for any one browse; enough for a few "Load more" pages.
POOL = 96

# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

_extra_items: dict[str, list[StoreItem]] = {}
_by_id: dict[str, StoreItem] = {}


def _generate(provider: str, n: int) -> StoreItem:
    r = random.Random(n * 977 + (0 if provider == "Wallhaven" else 5))
    k = n - len(data.STORE[provider])
    style = _STYLE_ORDER[(k + (0 if provider == "Wallhaven" else 3)) % len(_STYLE_ORDER)]
    turn = k // len(_STYLE_ORDER)
    titles = STYLE_TITLES[style]
    title = titles[(turn + (0 if provider == "Wallhaven" else 4)) % len(titles)]
    lowered = title.lower()
    if any(word in lowered for word in ("night", "moon", "star", "dusk", "blue hour", "lantern", "neon", "torch")):
        night = True
    elif any(word in lowered for word in ("noon", "morning", "sunrise", "sun", "salt", "light")):
        night = False
    else:
        night = style == "aurora" or (turn + k) % 2 == 1
    moving = provider == "MotionBGS"
    if moving:
        resolution = "3840 × 2160" if r.random() < 0.6 else "1920 × 1080"
        detail = f"0:{r.randint(10, 58):02d} · loop"
    else:
        resolution = (
            "2160 × 3840"
            if k % 19 == 7
            else "1440 × 2560"
            if k % 19 == 15
            else r.choices(
                [
                    "3840 × 2160",
                    "5120 × 2880",
                    "2560 × 1440",
                    "1920 × 1080",
                    "3440 × 1440",
                    "5120 × 1440",
                    "2560 × 1600",
                    "1440 × 2560",
                ],
                weights=[7, 2, 3, 2, 2, 1, 1, 1],
            )[0]
        )
        detail = f"{r.randint(14, 1400)} favorites"
    return StoreItem(
        id=f"{provider.lower()}-{n}",
        title=title,
        provider=provider,
        style=style,
        seed=300 + n * 11 + (0 if provider == "Wallhaven" else 3),
        night=night,
        resolution=resolution,
        detail=detail,
        tags=(style, "night" if night else "day", "live" if moving else "4k"),
    )


def item_at(provider: str, n: int) -> StoreItem:
    base = data.STORE[provider]
    if n < len(base):
        item = base[n]
    else:
        extra = _extra_items.setdefault(provider, [])
        while len(extra) <= n - len(base):
            extra.append(_generate(provider, len(base) + len(extra)))
        item = extra[n - len(base)]
    _by_id[item.id] = item
    return item


def pool(provider: str) -> list[StoreItem]:
    return [item_at(provider, n) for n in range(POOL)]


def by_id(item_id: str) -> StoreItem | None:
    if item_id not in _by_id:
        for provider in PROVIDERS:
            pool(provider)
    return _by_id.get(item_id)


# ---------------------------------------------------------------------------
# Facts derived from an item
# ---------------------------------------------------------------------------


def _rng(item: StoreItem) -> random.Random:
    return random.Random(item.seed * 31 + len(item.title))


def site_id(item: StoreItem) -> str:
    if item.provider == "MotionBGS":
        return item.title.lower().replace(" ", "-")
    return "".join(_rng(item).choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))


def url(item: StoreItem) -> str:
    if item.provider == "MotionBGS":
        return f"motionbgs.com/{site_id(item)}"
    return f"wallhaven.cc/w/{site_id(item)}"


def size(item: StoreItem) -> tuple[int, int]:
    width, height = item.resolution.replace(" ", "").split("×")
    if item.provider == "MotionBGS":
        return (3840, 2160) if has_4k(item) else (1920, 1080)
    return int(width), int(height)


def has_4k(item: StoreItem) -> bool:
    """MotionBGS offers HD and, for most loops, 4K."""
    if item.provider != "MotionBGS":
        return False
    return item.resolution.startswith("3840") or (item.resolution.startswith("2560") and item.seed % 3 != 0)


def ratio(item: StoreItem) -> str:
    width, height = size(item)
    if height > width:
        return "Portrait"
    value = width / height
    for label, target in (("16:9", 16 / 9), ("16:10", 16 / 10), ("21:9", 21 / 9), ("32:9", 32 / 9)):
        if abs(value - target) < 0.08:
            return label
    return f"{width}:{height}"


def short_resolution(item: StoreItem) -> str:
    """The badge on a card: 5K, 4K, 1440p … plus the ratio when it isn't 16:9."""
    width, height = size(item)
    if item.provider == "MotionBGS":
        return "4K" if has_4k(item) else "HD"
    short_side = min(width, height)
    label = "5K" if short_side >= 2880 else "4K" if short_side >= 2160 else f"{short_side}p"
    shape = ratio(item)
    return label if shape == "16:9" else f"{label} · {shape}"


def favorites(item: StoreItem) -> int:
    if item.provider == "MotionBGS":
        return 0
    return int(item.detail.split()[0])


def views(item: StoreItem) -> int:
    return favorites(item) * 23 + _rng(item).randint(200, 4000)


def duration(item: StoreItem) -> str:
    return item.detail.split(" ")[0] if item.provider == "MotionBGS" else ""


def megabytes(item: StoreItem, quality: str | None = None) -> float:
    r = _rng(item)
    if item.provider == "MotionBGS":
        seconds = int(duration(item).split(":")[1]) + 60 * int(duration(item).split(":")[0])
        hd = 0.9 * seconds + r.uniform(4, 12)
        return round(hd * 3.1 if (quality or ("4k" if has_4k(item) else "hd")) == "4k" else hd, 1)
    width, height = size(item)
    return round(width * height / 1_000_000 * r.uniform(0.6, 1.1), 1)


def file_type(item: StoreItem) -> str:
    if item.provider == "MotionBGS":
        return "MP4 video"
    return "PNG" if _rng(item).random() < 0.3 else "JPEG"


def category(item: StoreItem) -> str:
    """Wallhaven category or MotionBGS genre key."""
    if item.provider == "MotionBGS":
        return {
            "peaks": "nature",
            "ocean": "nature",
            "dunes": "nature",
            "rain": "nature",
            "city": "cyberpunk" if item.night else "anime",
            "forest": "fantasy" if item.night else "nature",
            "aurora": "space",
            "pond": "animals",
            "blocks": "games",
            "abstract": "abstract",
        }[item.style]
    if item.style == "blocks" or (item.style == "city" and item.seed % 2):
        return "Anime"
    return "General"


def category_label(item: StoreItem) -> str:
    value = category(item)
    return dict(MOTION_CATEGORIES).get(value, value)


def source(item: StoreItem) -> str:
    """Where the uploader says the picture comes from (Wallhaven's "Source")."""
    if item.provider == "MotionBGS":
        return "motionbgs.com"
    return ["artstation.com", "deviantart.com", "pixiv.net", "Not given", "unsplash.com"][item.seed % 5]


def uploader(item: StoreItem) -> str:
    return _rng(item).choice(UPLOADERS)


def added(item: StoreItem) -> str:
    return ADDED[(item.seed // 7) % len(ADDED)]


def age_rank(item: StoreItem) -> int:
    return (item.seed // 7) % len(ADDED)


def tags(item: StoreItem) -> list[str]:
    seen: list[str] = []
    for tag in (*STYLE_TAGS.get(item.style, ()), *item.tags):
        if tag not in seen and tag not in ("4k", "live", "day"):
            seen.append(tag)
    return seen


def colors(item: StoreItem) -> list[str]:
    look = art.look_for(item.style, item.seed, item.night)
    return [art.to_hex(c) for c in (look.sky_top, look.sky_low, look.accent, look.land)]


def nearest_palette_color(hex_color: str) -> str:
    """The Wallhaven search color closest to ``hex_color``."""

    def rgb(value: str) -> tuple[float, float, float]:
        value = value.lstrip("#")
        return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))

    target = rgb(hex_color)
    return min(COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb(c), target, strict=True)))


def matches_color(item: StoreItem, hex_color: str) -> bool:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if sat < 0.15 or value == "424153":
        return item.night if light < 0.5 else (not item.night and item.style in ("peaks", "dunes", "abstract"))
    # The sun or moon (the look's accent) is too small to count as a main color.
    look = art.look_for(item.style, item.seed, item.night)
    for color in (art.to_hex(c) for c in (look.sky_top, look.sky_low, look.land)):
        cr, cg, cb = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
        ch, _cl, cs = colorsys.rgb_to_hls(cr, cg, cb)
        distance = min(abs(ch - hue), 1 - abs(ch - hue))
        if cs > 0.25 and distance < 0.06:
            return True
    return False


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@dataclass
class Query:
    provider: str
    text: str = ""
    sort: str = "toplist"
    top_range: str = "1w"
    reverse: bool = False
    min_height: int = 0
    ratio: str = "any"
    color: str | None = None
    categories: frozenset[str] = frozenset(CATEGORIES)
    genre: str = "all"
    quality: str = "any"


def like_source(text: str) -> StoreItem | None:
    """The item a Wallhaven ``like:<id>`` query refers to."""
    wanted = text.strip()[5:]
    for provider in PROVIDERS:
        for item in pool(provider):
            if site_id(item) == wanted or item.id == wanted:
                return item
    return None


def _matches_text(item: StoreItem, text: str) -> bool:
    haystack = " ".join((item.title, " ".join(tags(item)), item.style, category_label(item))).lower()
    return all(word in haystack for word in text.lower().split())


def search(query: Query) -> list[StoreItem]:
    items = pool(query.provider)
    text = query.text.strip()
    if text.startswith("like:"):
        source = like_source(text)
        items = [i for i in items if source and i.style == source.style and i.id != source.id]
    elif text:
        items = [i for i in items if _matches_text(i, text)]
    if query.provider == "Wallhaven":
        items = [i for i in items if category(i) in query.categories]
        if query.min_height:
            items = [i for i in items if min(size(i)) >= query.min_height]
        if query.ratio != "any":
            items = [i for i in items if ratio(i).lower() == query.ratio.lower()]
        if query.color:
            items = [i for i in items if matches_color(i, query.color)]
        key = {
            "date_added": age_rank,
            "views": lambda i: -views(i),
            "favorites": lambda i: -favorites(i),
            "hot": lambda i: -(favorites(i) // (1 + age_rank(i))),
            "random": lambda i: random.Random(i.seed * 13 + 7).random(),
        }.get(query.sort)
        if query.sort == "toplist" and query.top_range != "1w":
            offset = [r[0] for r in RANGES].index(query.top_range) + 1
            key = lambda i: random.Random(i.seed * offset).random()  # noqa: E731 - a different chart
        if key:
            items = sorted(items, key=key)
        if query.reverse and query.sort not in ("random", "relevance"):
            items.reverse()
    else:
        if query.genre != "all" and not text:
            items = [i for i in items if category(i) == query.genre]
        if query.quality == "4k":
            items = [i for i in items if has_4k(i)]
    return items
