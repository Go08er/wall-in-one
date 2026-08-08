"""Wallhaven: still wallpapers, over a documented JSON API.

An API is easier than MotionBGS's HTML, and the defence moves accordingly: the
work here is in refusing to believe the JSON. Everything the API returns is
decoded as `object` and walked into frozen dataclasses by the helpers at the
top of this file; nothing typed `Any` reaches a caller, a filename, or the
disk.

Three things carry over from the previous implementation because they were
learned the hard way:

* **The CDN URL is derived, not trusted.** `wallhaven-<id>.<ext>` under
  `w.wallhaven.cc/full/<first two characters of id>/` is the only shape
  accepted, and the id inside it must match the id of the record carrying it.
  Anything else is a result that could point the downloader at another host's
  file, so the whole result is dropped.
* **NSFW needs a key.** The purity mask's third bit is refused without one --
  before any request, because sending it anyway just gets a silent
  downgrade and a confusing empty grid.
* **Downloaded pixels must match the advertised metadata.** Byte count, MIME
  type, *and* the dimensions read out of the file's own headers. That is what
  the JPEG and PNG readers below are for; they parse structure only, and never
  a pixel.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from urllib.parse import quote

from wall_in_one.library.model import Kind
from wall_in_one.providers import download as download_module
from wall_in_one.providers import http
from wall_in_one.providers.base import (
    MAX_RESULTS,
    DownloadResult,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)
from wall_in_one.providers.cache import TtlCache

API_ORIGIN: Final = "https://wallhaven.cc/api/v1"
SITE_ORIGIN: Final = "https://wallhaven.cc"
SHORT_ORIGIN: Final = "https://whvn.cc"
CDN_ORIGIN: Final = "https://w.wallhaven.cc"
THUMB_ORIGIN: Final = "https://th.wallhaven.cc"

MAX_TAGS: Final = 48
MAX_QUERY_BYTES: Final = 256
MAX_TEXT_BYTES: Final = 512
MAX_URL_BYTES: Final = 2048
MAX_BODY_BYTES: Final = 512 * 1024
MAX_DOWNLOAD_BYTES: Final = 64 * 1024 * 1024
MAX_PIXELS: Final = 512 * 1024 * 1024
MAX_DIMENSION: Final = 100_000

#: Wallhaven documents 45 requests a minute; two seconds apart stays inside it
#: with room for the UI to fire a couple of calls back to back.
MIN_API_INTERVAL_SECONDS: Final = 2.0
API_TIMEOUT_SECONDS: Final = 30.0
MEDIA_TIMEOUT_SECONDS: Final = 120.0

ID_RE: Final = re.compile(r"^[a-z0-9]{6}$")
CDN_RE: Final = re.compile(
    r"^https://w\.wallhaven\.cc/full/([a-z0-9]{2})/wallhaven-([a-z0-9]{6})\.(jpg|png)$"
)
API_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

SORTING: Final[frozenset[str]] = frozenset(
    {"date_added", "relevance", "random", "views", "favorites", "toplist", "hot"}
)
ORDER: Final[frozenset[str]] = frozenset({"asc", "desc"})
TOP_RANGE: Final[frozenset[str]] = frozenset({"1d", "3d", "1w", "1M", "3M", "6M", "1y"})
#: Wallhaven's palette, in Wallhaven's own order -- reds through pinks,
#: purples, blues, greens, yellows, oranges, browns, then the neutrals. A
#: sequence rather than a set because a colour picker has to lay them out, and
#: a set would rearrange the swatches between runs.
COLOR_ORDER: Final[tuple[str, ...]] = (
    "660000", "990000", "cc0000", "cc3333", "ea4c88", "993399",
    "663399", "333399", "0066cc", "0099cc", "66cccc", "77cc33",
    "669900", "336600", "666600", "999900", "cccc33", "ffff00",
    "ffcc33", "ff9900", "ff6600", "cc6633", "996633", "663300",
    "000000", "999999", "cccccc", "ffffff", "424153",
)  # fmt: skip

COLORS: Final[frozenset[str]] = frozenset(COLOR_ORDER)

FILTER_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "categories",
        "purity",
        "sorting",
        "order",
        "atleast",
        "resolutions",
        "ratios",
        "colors",
        "top_range",
        "seed",
    }
)


# -- reading untrusted JSON ----------------------------------------------


def as_mapping(value: object, what: str) -> Mapping[str, object]:
    """Narrow a decoded JSON value to a string-keyed mapping, or refuse it."""
    if not isinstance(value, dict):
        raise ProviderError("response", f"{what} is not an object")
    narrowed: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            narrowed[key] = item
    return narrowed


def as_sequence(value: object, what: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ProviderError("response", f"{what} is not a list")
    return list(value)


def text_field(value: object, maximum: int) -> str:
    """A control-free string, truncated on a UTF-8 boundary."""
    if not isinstance(value, str):
        return ""
    flattened = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character for character in value
    ).strip()
    encoded = flattened.encode("utf-8")
    if len(encoded) <= maximum:
        return flattened
    return encoded[:maximum].decode("utf-8", errors="ignore")


def integer_field(value: object, fallback: int, minimum: int, maximum: int) -> int:
    """Clamp a JSON number into range, falling back when it is not one.

    `bool` is an `int` in Python but is never a number in this API, and JSON
    admits infinities and NaN through some encoders, so both are excluded.
    """
    number = fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        as_float = float(value)
        if math.isfinite(as_float):
            number = math.floor(as_float)
    return max(minimum, min(maximum, number))


# -- identity and URLs ---------------------------------------------------


def normalise_id(value: object) -> str:
    if not isinstance(value, str) or ID_RE.fullmatch(value.lower()) is None:
        raise ProviderError("validation", "wallpaper ID must be six alphanumeric characters")
    return value.lower()


def cdn_url(value: object, identifier: str) -> tuple[str, str]:
    """The full-size URL and its extension, derived and cross-checked.

    The shard directory must be the first two characters of the id and the
    filename must carry the same id: a result whose media URL points at another
    wallpaper -- or another host -- is refused outright rather than repaired.
    """
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_URL_BYTES:
        raise ProviderError("response", "wallpaper result has an unsupported media URL")
    match = CDN_RE.fullmatch(value)
    if match is None or match.group(1) != identifier[:2] or match.group(2) != identifier:
        raise ProviderError("response", "wallpaper result has an unsupported media URL")
    return value, match.group(3)


def thumb_url(value: object) -> str:
    """A thumbnail URL, or ``""``. A missing thumbnail is not worth failing on."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_URL_BYTES:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    if "?" in value or "#" in value or not value.startswith(THUMB_ORIGIN + "/"):
        return ""
    path = value[len(THUMB_ORIGIN) + 1 :]
    if (
        not path
        or "//" in path
        or any(component in {".", ".."} for component in path.split("/"))
        or re.fullmatch(r"[A-Za-z0-9._/-]+", path) is None
    ):
        return ""
    return THUMB_ORIGIN + "/" + path


# -- parsed documents ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class WallhavenTag:
    identifier: int
    name: str
    alias: str
    category: str
    purity: str


@dataclass(frozen=True, slots=True)
class WallhavenWallpaper:
    identifier: str
    page_url: str
    short_url: str
    media_url: str
    extension: str
    file_type: str
    file_size: int
    width: int
    height: int
    resolution: str
    ratio: str
    purity: str
    category: str
    views: int
    favorites: int
    source: str
    created_at: str
    colors: tuple[str, ...] = ()
    thumbnail_large: str = ""
    thumbnail_small: str = ""
    thumbnail_original: str = ""
    tags: tuple[WallhavenTag, ...] = ()
    uploader: str = ""

    @property
    def filename(self) -> str:
        return f"wallhaven-{self.identifier}.{self.extension}"

    def to_candidate(self) -> WallpaperCandidate:
        return WallpaperCandidate(
            provider=Wallhaven.name,
            identifier=self.identifier,
            title=self.identifier if not self.tags else self.tags[0].name,
            kind=Kind.STILL,
            page_url=self.page_url,
            thumbnail_url=self.thumbnail_large or self.thumbnail_small,
            resolution=self.resolution,
            size_hint=self.file_size,
        )


def normalise_wallpaper(value: object, *, detail: bool) -> WallhavenWallpaper:
    record = as_mapping(value, "wallpaper result")
    identifier = normalise_id(record.get("id"))
    media_url, extension = cdn_url(record.get("path"), identifier)
    file_type = text_field(record.get("file_type"), 32).lower()
    expected_type = "image/jpeg" if extension == "jpg" else "image/png"
    if file_type != expected_type:
        raise ProviderError("response", "wallpaper result file type does not match its URL")
    width = integer_field(record.get("dimension_x"), 0, 0, 1_000_000)
    height = integer_field(record.get("dimension_y"), 0, 0, 1_000_000)
    thumbs = record.get("thumbs")
    thumb_map: Mapping[str, object] = thumbs if isinstance(thumbs, dict) else {}
    wallpaper = WallhavenWallpaper(
        identifier=identifier,
        page_url=f"{SITE_ORIGIN}/w/{identifier}",
        short_url=f"{SHORT_ORIGIN}/{identifier}",
        media_url=media_url,
        extension=extension,
        file_type=file_type,
        file_size=integer_field(record.get("file_size"), 0, 0, 2**53 - 1),
        width=width,
        height=height,
        resolution=(
            f"{width}x{height}"
            if width > 0 and height > 0
            else text_field(record.get("resolution"), 32)
        ),
        ratio=text_field(record.get("ratio"), 32),
        purity=text_field(record.get("purity"), 16),
        category=text_field(record.get("category"), 32),
        views=integer_field(record.get("views"), 0, 0, 2**53 - 1),
        favorites=integer_field(record.get("favorites"), 0, 0, 2**53 - 1),
        source=text_field(record.get("source"), MAX_TEXT_BYTES),
        created_at=text_field(record.get("created_at"), 64),
        colors=_colors(record.get("colors")),
        thumbnail_large=thumb_url(thumb_map.get("large")),
        thumbnail_small=thumb_url(thumb_map.get("small")),
        thumbnail_original=thumb_url(thumb_map.get("original")),
    )
    if not detail:
        return wallpaper
    uploader = record.get("uploader")
    return replace(
        wallpaper,
        tags=_tags(record.get("tags")),
        uploader=(text_field(uploader.get("username"), 80) if isinstance(uploader, dict) else ""),
    )


def _colors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    colors: list[str] = []
    for candidate in value:
        if len(colors) >= 8:
            break
        if isinstance(candidate, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", candidate):
            colors.append(candidate.lower())
    return tuple(colors)


def _tags(value: object) -> tuple[WallhavenTag, ...]:
    if not isinstance(value, list):
        return ()
    tags: list[WallhavenTag] = []
    for candidate in value:
        if len(tags) >= MAX_TAGS:
            break
        if not isinstance(candidate, dict):
            continue
        identifier = integer_field(candidate.get("id"), 0, 0, 2**53 - 1)
        name = text_field(candidate.get("name"), 160)
        if identifier > 0 and name:
            tags.append(
                WallhavenTag(
                    identifier=identifier,
                    name=name,
                    alias=text_field(candidate.get("alias"), 160),
                    category=text_field(candidate.get("category"), 80),
                    purity=text_field(candidate.get("purity"), 16),
                )
            )
    return tuple(tags)


# -- filters -------------------------------------------------------------


def _bits(value: str, fallback: str, label: str) -> str:
    bits = fallback if value == "" else value
    if re.fullmatch(r"[01]{3}", bits) is None or bits == "000":
        raise ProviderError("validation", f"{label} must be a three-bit Wallhaven mask")
    return bits


def parse_resolution(value: str) -> str:
    if value == "":
        return ""
    if len(value.encode("utf-8")) > 24:
        raise ProviderError("validation", "minimum resolution must look like 1920x1080")
    match = re.fullmatch(r"([0-9]+)x([0-9]+)", value)
    if match is None:
        raise ProviderError("validation", "minimum resolution must look like 1920x1080")
    width, height = int(match.group(1)), int(match.group(2))
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise ProviderError("validation", "minimum resolution must look like 1920x1080")
    return f"{width}x{height}"


def parse_resolution_list(value: str) -> str:
    if value == "":
        return ""
    if len(value.encode("utf-8")) > 192 or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ProviderError("validation", "exact resolutions must be comma-separated values")
    parts = value.split(",")
    if not 1 <= len(parts) <= 12:
        raise ProviderError("validation", "resolutions accepts at most twelve exact values")
    if "" in parts:
        # `parse_resolution` reads an empty string as "no filter", which is right
        # for a lone `atleast` and wrong inside a list: `1920x1080,` would
        # otherwise round-trip unchanged below and reach the API as a stray comma.
        raise ProviderError("validation", "exact resolutions must be comma-separated values")
    normalised = ",".join(parse_resolution(part) for part in parts)
    # Round-tripping is the check: `1920x1080,` and `01920x1080` both normalise
    # to something else, and a filter the user cannot see is worse than an error.
    if normalised != value:
        raise ProviderError("validation", "exact resolutions must be comma-separated values")
    return normalised


def parse_ratio_list(value: str) -> str:
    if value == "":
        return ""
    message = "ratios must be a comma-separated list such as 16x9,16x10"
    if len(value.encode("utf-8")) > 96 or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ProviderError("validation", message)
    parts = value.split(",")
    if not 1 <= len(parts) <= 8:
        raise ProviderError("validation", "ratios accepts at most eight values")
    normalised: list[str] = []
    for part in parts:
        match = re.fullmatch(r"([0-9]+)x([0-9]+)", part)
        if match is None:
            raise ProviderError("validation", message)
        width, height = int(match.group(1)), int(match.group(2))
        if not (1 <= width <= 1000 and 1 <= height <= 1000):
            raise ProviderError("validation", message)
        normalised.append(f"{width}x{height}")
    result = ",".join(normalised)
    if result != value:
        raise ProviderError("validation", message)
    return result


@dataclass(frozen=True, slots=True)
class WallhavenFilters:
    """A validated Wallhaven search, ready to be turned into a URL."""

    query: str = ""
    categories: str = "111"
    purity: str = "100"
    sorting: str = "date_added"
    order: str = "desc"
    atleast: str = ""
    resolutions: str = ""
    ratios: str = ""
    colors: str = ""
    top_range: str = "1M"
    seed: str = ""
    page: int = 1

    @classmethod
    def from_query(cls, query: SearchQuery, *, authenticated: bool) -> WallhavenFilters:
        unknown = set(query.options) - FILTER_OPTIONS
        if unknown:
            raise ProviderError(
                "invalid-request",
                f"unsupported Wallhaven options: {', '.join(sorted(unknown))}",
            )
        text = query.text
        if len(text.encode("utf-8")) > MAX_QUERY_BYTES or any(
            ord(character) < 32 or ord(character) == 127 for character in text
        ):
            raise ProviderError(
                "validation",
                f"query must be control-free text no longer than {MAX_QUERY_BYTES} bytes",
            )
        purity = _bits(query.option("purity"), "100", "purity")
        if purity[2] == "1" and not authenticated:
            raise ProviderError("credential", "NSFW search requires a Wallhaven API key")
        sorting = query.option("sorting", "date_added").lower()
        if sorting not in SORTING:
            raise ProviderError("validation", "sorting is not supported by Wallhaven API v1")
        order = query.option("order", "desc").lower()
        if order not in ORDER:
            raise ProviderError("validation", "order must be asc or desc")
        atleast = parse_resolution(query.option("atleast"))
        resolutions = parse_resolution_list(query.option("resolutions"))
        if atleast and resolutions:
            raise ProviderError(
                "validation",
                "choose either a minimum resolution or exact resolutions, not both",
            )
        raw_color = query.option("colors")
        if raw_color and raw_color.lower().lstrip("#") not in COLORS:
            raise ProviderError("validation", "colors must be one of Wallhaven's documented values")
        top_range = query.option("top_range", "1M")
        if top_range not in TOP_RANGE:
            raise ProviderError("validation", "top_range has an unsupported value")
        seed = query.option("seed")
        if seed and (sorting != "random" or re.fullmatch(r"[A-Za-z0-9]{6}", seed) is None):
            raise ProviderError(
                "validation",
                "seed must be six alphanumeric characters used with random sorting",
            )
        if not 1 <= query.page <= 10_000:
            raise ProviderError("validation", "page must be an integer from 1 through 10000")
        return cls(
            query=text.strip(),
            categories=_bits(query.option("categories"), "111", "categories"),
            purity=purity,
            sorting=sorting,
            order=order,
            atleast=atleast,
            resolutions=resolutions,
            ratios=parse_ratio_list(query.option("ratios")),
            colors=raw_color.lower().lstrip("#") if raw_color else "",
            top_range=top_range,
            seed=seed,
            page=query.page,
        )

    def search_url(self) -> str:
        pairs: list[tuple[str, str]] = [
            (name, value)
            for name, value in (
                ("q", self.query),
                ("categories", self.categories),
                ("purity", self.purity),
                ("sorting", self.sorting),
                ("order", self.order),
                ("atleast", self.atleast),
                ("resolutions", self.resolutions),
                ("ratios", self.ratios),
                ("colors", self.colors),
            )
            if value != ""
        ]
        if self.sorting == "toplist":
            pairs.append(("topRange", self.top_range))
        elif self.sorting == "random" and self.seed:
            pairs.append(("seed", self.seed))
        pairs.append(("page", str(self.page)))
        encoded = "&".join(
            quote(name, safe="-._~") + "=" + quote(value, safe="-._~") for name, value in pairs
        )
        return f"{API_ORIGIN}/search?{encoded}"

    def cache_key(self) -> str:
        return "\0".join(
            (
                self.query,
                self.categories,
                self.purity,
                self.sorting,
                self.order,
                self.atleast,
                self.resolutions,
                self.ratios,
                self.colors,
                self.top_range,
                self.seed,
                str(self.page),
            )
        )


# -- image structure -----------------------------------------------------

_SOF_MARKERS: Final[frozenset[int]] = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_PNG_DEPTHS: Final[Mapping[int, frozenset[int]]] = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}


def jpeg_dimensions(path: Path) -> tuple[int, int]:
    """Width and height from the JPEG's own frame header.

    Walks the segment chain rather than trusting any single offset, and is
    bounded on segment count so a crafted file cannot spin here.
    """
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise ProviderError("content-type", "JPEG signature is invalid")
        stream.seek(-2, 2)
        if stream.read(2) != b"\xff\xd9":
            raise ProviderError("content-type", "JPEG is missing its terminal marker")
        position = 2
        dimensions: tuple[int, int] | None = None
        segments = 0
        while position < size - 2:
            segments += 1
            if segments > 4096:
                raise ProviderError("content-type", "JPEG contains too many segments")
            stream.seek(position)
            if stream.read(1) != b"\xff":
                raise ProviderError("content-type", "JPEG segment framing is invalid")
            marker_byte = stream.read(1)
            fills = 0
            while marker_byte == b"\xff":
                fills += 1
                if fills > 64:
                    raise ProviderError("content-type", "JPEG marker fill is excessive")
                marker_byte = stream.read(1)
            if not marker_byte:
                break
            marker = marker_byte[0]
            position = stream.tell()
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if marker in {0x00, 0xD8, 0xD9}:
                raise ProviderError("content-type", "JPEG contains an invalid marker")
            raw_length = stream.read(2)
            if len(raw_length) != 2:
                raise ProviderError("content-type", "JPEG segment is truncated")
            length = struct.unpack(">H", raw_length)[0]
            end = position + length
            if length < 2 or end > size - 2:
                raise ProviderError("content-type", "JPEG segment length is invalid")
            if marker in _SOF_MARKERS:
                payload = stream.read(min(length - 2, 16))
                if len(payload) < 6:
                    raise ProviderError("content-type", "JPEG frame header is truncated")
                height, width = struct.unpack(">HH", payload[1:5])
                components = payload[5]
                if not 1 <= components <= 4 or length != 8 + 3 * components:
                    raise ProviderError("content-type", "JPEG frame header is invalid")
                if dimensions is not None:
                    raise ProviderError("content-type", "JPEG contains multiple frame headers")
                dimensions = (width, height)
            elif marker == 0xDA:
                payload = stream.read(min(length - 2, 16))
                if dimensions is None or len(payload) < 1:
                    raise ProviderError("content-type", "JPEG scan precedes its frame header")
                components = payload[0]
                if not 1 <= components <= 4 or length != 6 + 2 * components:
                    raise ProviderError("content-type", "JPEG scan header is invalid")
                if end >= size - 2:
                    raise ProviderError("content-type", "JPEG has no entropy-coded image data")
                return dimensions
            position = end
    raise ProviderError("content-type", "JPEG has no complete image scan")


def png_dimensions(path: Path) -> tuple[int, int]:
    """Width and height from IHDR, with every chunk's CRC verified."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ProviderError("content-type", "PNG signature is invalid")
        position = 8
        chunks = 0
        dimensions: tuple[int, int] | None = None
        saw_data = False
        saw_end = False
        while position < size:
            chunks += 1
            if chunks > 4096:
                raise ProviderError("content-type", "PNG contains too many chunks")
            header = stream.read(8)
            if len(header) != 8:
                raise ProviderError("content-type", "PNG chunk header is truncated")
            length, chunk_type = struct.unpack(">I4s", header)
            position += 8
            if length > size - position - 4 or re.fullmatch(rb"[A-Za-z]{4}", chunk_type) is None:
                raise ProviderError("content-type", "PNG chunk framing is invalid")
            checksum = zlib.crc32(chunk_type)
            remaining = length
            prefix = b""
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ProviderError("content-type", "PNG chunk is truncated")
                if len(prefix) < 13:
                    prefix += chunk[: 13 - len(prefix)]
                checksum = zlib.crc32(chunk, checksum)
                remaining -= len(chunk)
            expected = stream.read(4)
            if len(expected) != 4 or struct.unpack(">I", expected)[0] != checksum & 0xFFFFFFFF:
                raise ProviderError("content-type", "PNG chunk checksum is invalid")
            position += length + 4
            if chunks == 1:
                if chunk_type != b"IHDR" or length != 13:
                    raise ProviderError("content-type", "PNG must start with one IHDR chunk")
                width, height, depth, color, compression, filtering, interlace = struct.unpack(
                    ">IIBBBBB", prefix
                )
                if (
                    color not in _PNG_DEPTHS
                    or depth not in _PNG_DEPTHS[color]
                    or compression != 0
                    or filtering != 0
                    or interlace not in {0, 1}
                ):
                    raise ProviderError("content-type", "PNG image header is unsupported")
                dimensions = (width, height)
            elif chunk_type == b"IHDR":
                raise ProviderError("content-type", "PNG contains multiple IHDR chunks")
            if chunk_type == b"IDAT":
                if length < 1:
                    raise ProviderError("content-type", "PNG image data is empty")
                saw_data = True
            elif chunk_type == b"IEND":
                if length != 0 or not saw_data or position != size:
                    raise ProviderError("content-type", "PNG terminal chunk is invalid")
                saw_end = True
                break
        if dimensions is None or not saw_end:
            raise ProviderError("content-type", "PNG image structure is incomplete")
        return dimensions


def validate_image(path: Path, content_type: str, wallpaper: WallhavenWallpaper) -> tuple[int, str]:
    """Check the file against the metadata that authorised downloading it."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ProviderError("local-io", f"could not read the download: {error}") from error
    if size != wallpaper.file_size:
        raise ProviderError("size-mismatch", "downloaded size does not match Wallhaven metadata")
    if content_type != wallpaper.file_type:
        raise ProviderError("content-type", "download MIME type does not match Wallhaven metadata")
    dimensions = (
        jpeg_dimensions(path) if wallpaper.file_type == "image/jpeg" else png_dimensions(path)
    )
    if dimensions != (wallpaper.width, wallpaper.height):
        raise ProviderError(
            "dimensions", "downloaded image dimensions do not match Wallhaven metadata"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return size, digest.hexdigest()


# -- the provider --------------------------------------------------------


def normalise_api_key(value: str) -> str:
    """Accept a key, or refuse one that could not be a key."""
    key = value.strip()
    if not key:
        return ""
    if API_KEY_RE.fullmatch(key) is None:
        raise ProviderError("credential", "Wallhaven API key has an unexpected shape")
    return key


class Wallhaven:
    """Still wallpapers from wallhaven.cc."""

    name: str = "wallhaven"
    title: str = "Wallhaven"
    media_kind: Kind = Kind.STILL

    def __init__(
        self,
        client: http.Client | None = None,
        *,
        api_key: str = "",
        rate_limiter: http.RateLimiter | None = None,
    ) -> None:
        self._client: http.Client = client if client is not None else http.UrllibClient()
        self._api_key = normalise_api_key(api_key)
        self._rate = (
            rate_limiter if rate_limiter is not None else http.RateLimiter(MIN_API_INTERVAL_SECONDS)
        )
        self._searches: TtlCache[tuple[SearchResult, int]] = TtlCache(max_entries=16)
        self._details: TtlCache[WallhavenWallpaper] = TtlCache(max_entries=64)

    @property
    def authenticated(self) -> bool:
        return bool(self._api_key)

    # -- transport -------------------------------------------------------

    def _api_headers(self) -> tuple[tuple[str, str], ...]:
        return (("X-API-Key", self._api_key),) if self._api_key else ()

    def _api_json(self, url: str) -> object:
        self._rate.wait()
        response = self._client.fetch(
            http.Request(
                url=url,
                accept="application/json",
                timeout=API_TIMEOUT_SECONDS,
                max_bytes=MAX_BODY_BYTES,
                headers=self._api_headers(),
            )
        )
        if response.is_redirect:
            # Wallhaven's API does not redirect. One that does is not it.
            raise ProviderError("redirects", "Wallhaven redirected an API request")
        _refuse_error_status(response.status)
        if response.content_type != "application/json":
            raise ProviderError(
                "content-type",
                f"unexpected response type {response.content_type or 'unknown'}",
            )
        try:
            decoded: object = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ProviderError("response", f"Wallhaven returned invalid JSON: {error}") from error
        return decoded

    # -- search ----------------------------------------------------------

    def search(self, query: SearchQuery) -> SearchResult:
        filters = WallhavenFilters.from_query(query, authenticated=self.authenticated)
        return self.search_with(filters)

    def search_with(self, filters: WallhavenFilters) -> SearchResult:
        key = filters.cache_key()
        cached = self._searches.get(key)
        if cached is not None:
            result, _ = cached
            return replace(result, cached=True)

        url = filters.search_url()
        decoded = self._api_json(url)
        document = as_mapping(decoded, "Wallhaven search response")
        data = document.get("data")
        if not isinstance(data, list):
            raise ProviderError(
                "response", "Wallhaven search response did not include a result list"
            )
        items: list[WallpaperCandidate] = []
        dropped = 0
        for entry in data:
            if len(items) >= MAX_RESULTS:
                break
            try:
                wallpaper = normalise_wallpaper(entry, detail=False)
            except ProviderError:
                # One bad record is not a bad page. Counted so a schema change
                # shows up as a number rather than as a mysteriously short grid.
                dropped += 1
                continue
            self._details.put(wallpaper.identifier, wallpaper)
            items.append(wallpaper.to_candidate())

        raw_meta = document.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        current_page = integer_field(meta.get("current_page"), filters.page, 1, 10_000)
        last_page = integer_field(meta.get("last_page"), filters.page, 1, 2**53 - 1)
        result = SearchResult(
            provider=self.name,
            query_url=url,
            items=tuple(items),
            page=current_page,
            has_previous=current_page > 1,
            has_next=current_page < last_page,
            total_hint=integer_field(meta.get("total"), len(items), 0, 2**53 - 1),
            dropped=dropped,
        )
        self._searches.put(key, (result, last_page))
        return result

    def detail(self, identifier: str) -> WallhavenWallpaper:
        identifier = normalise_id(identifier)
        cached = self._details.get(identifier)
        if cached is not None and cached.tags:
            return cached
        decoded = self._api_json(f"{API_ORIGIN}/w/{identifier}")
        document = as_mapping(decoded, "Wallhaven detail response")
        wallpaper = normalise_wallpaper(document.get("data"), detail=True)
        if wallpaper.identifier != identifier:
            raise ProviderError("response", "Wallhaven returned details for a different wallpaper")
        self._details.put(identifier, wallpaper)
        return wallpaper

    # -- download --------------------------------------------------------

    def download(
        self, candidate: WallpaperCandidate, root: Path, *, variant: str = ""
    ) -> DownloadResult:
        # Wallhaven publishes one file per wallpaper, so there is no variant to
        # choose; the parameter exists to satisfy the shared protocol.
        del variant
        wallpaper = self.detail(candidate.identifier)
        if not 1 <= wallpaper.file_size <= MAX_DOWNLOAD_BYTES:
            raise ProviderError("validation", "Wallhaven reported an implausible file size")
        if not 1 <= wallpaper.width <= MAX_DIMENSION or not 1 <= wallpaper.height <= MAX_DIMENSION:
            raise ProviderError("validation", "Wallhaven reported implausible dimensions")
        if wallpaper.width * wallpaper.height > MAX_PIXELS:
            raise ProviderError(
                "validation", "Wallhaven image dimensions exceed the safety ceiling"
            )

        directory, marker = download_module.managed_directory(
            root, download_module.WALLHAVEN_LOCATION
        )
        destination = download_module.safe_child(directory, wallpaper.filename)
        sidecar_suffix = download_module.WALLHAVEN_LOCATION.sidecar_suffix
        if destination.exists() or Path(str(destination) + sidecar_suffix).exists():
            raise ProviderError("conflict", f"{wallpaper.filename} is already in the library")

        transfer = self._client.download(
            http.Request(
                url=wallpaper.media_url,
                accept="image/jpeg,image/png;q=0.9",
                timeout=MEDIA_TIMEOUT_SECONDS,
                max_bytes=wallpaper.file_size,
            ),
            directory,
        )
        with transfer:
            if transfer.is_redirect:
                raise ProviderError("redirects", "Wallhaven redirected a media request")
            _refuse_error_status(transfer.status)
            if transfer.path is None:
                raise ProviderError("transport", "download produced no body")
            if transfer.url != wallpaper.media_url:
                raise ProviderError("redirects", "download resolved to a different Wallhaven URL")
            size, digest = validate_image(transfer.path, transfer.content_type, wallpaper)
            downloaded_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = download_module.encode_sidecar(
                {
                    "schema": 1,
                    "plugin": "goober/wall-in-one",
                    "provider": "Wallhaven",
                    "source": "wallhaven",
                    "id": wallpaper.identifier,
                    "url": wallpaper.media_url,
                    "path": str(destination),
                    "source_page": wallpaper.page_url,
                    "page_url": wallpaper.page_url,
                    "bytes": size,
                    "sha256": digest,
                    "downloaded_at": downloaded_at,
                }
            )
            installed, sidecar = download_module.install(
                transfer.path, destination, sidecar_suffix, payload
            )
        return DownloadResult(
            provider=self.name,
            identifier=wallpaper.identifier,
            path=installed,
            sidecar=sidecar,
            marker=marker,
            kind=Kind.STILL,
            size=size,
            source_url=wallpaper.page_url,
            download_url=wallpaper.media_url,
            sha256=digest,
            downloaded_at=downloaded_at,
        )

    def clear_cache(self) -> None:
        self._searches.clear()
        self._details.clear()


def _refuse_error_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status == 401:
        raise ProviderError("credential", "Wallhaven rejected the API key")
    if status == 429:
        raise ProviderError("rate-limit", "Wallhaven's API rate limit was reached")
    if status >= 500:
        raise ProviderError("remote", "Wallhaven is temporarily unavailable")
    raise ProviderError("http", f"Wallhaven returned HTTP {status}")
