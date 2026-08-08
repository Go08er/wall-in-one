"""MotionBGS: video wallpapers, scraped from public HTML.

There is no API, so this reads the site's markup. That is why almost all of
this file is defence rather than parsing: a scraper trusts a remote party's
HTML to name a file it will then write to disk, and the previous
implementation earned every one of these checks.

The shape of the defence, outermost first:

* :func:`normalise_url` -- every URL, from every source, is re-anchored on the
  MotionBGS origin and rejected unless it lands there. Percent-encoded
  separators are decoded *before* the traversal check, because `%2e%2e%2f` is a
  traversal and `urlsplit` will not tell you so.
* :func:`still_url` / :func:`video_url` -- the surviving URLs must additionally
  match the exact media route, so a valid MotionBGS URL pointing at an HTML
  page cannot be presented as an image.
* :class:`_BoundedParser` -- ceilings on tags, attributes per tag, and bytes
  per attribute, so a hostile page cannot exhaust memory during the parse.
* :func:`listing_route_matches` -- the page we got must be the page we asked
  for, or a redirect has quietly moved us into a different catalogue.
* :func:`validate_download_route` -- the bytes we received must have come from
  the media id the detail page authorised, not merely from the right host.
* :func:`validate_mp4` -- and they must actually be an MP4.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Final
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

from wall_in_one.library.model import Kind
from wall_in_one.providers import download as download_module
from wall_in_one.providers import http
from wall_in_one.providers.base import (
    MAX_RESULTS,
    CandidateDetail,
    DownloadResult,
    Fact,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
    human_bytes,
)
from wall_in_one.providers.cache import TtlCache

ORIGIN: Final = "https://motionbgs.com"
HOST: Final = "motionbgs.com"

#: Browse modes the site exposes. `search` has no pagination and `hd` is a
#: single curated page; both were established by observation, not documented.
MODES: Final[frozenset[str]] = frozenset({"search", "latest", "genre", "4k", "hd"})

MAX_HTML_BYTES: Final = 1024 * 1024
MAX_DOWNLOAD_BYTES: Final = 512 * 1024 * 1024
MAX_QUERY_BYTES: Final = 80
MAX_PAGE: Final = 10_000
MAX_REDIRECTS: Final = 3
MAX_HTML_TAGS: Final = 50_000
MAX_ATTRIBUTES_PER_TAG: Final = 64
MAX_ATTRIBUTE_BYTES: Final = 4096
MAX_URL_BYTES: Final = 2048

HTML_TIMEOUT_SECONDS: Final = 20.0
DOWNLOAD_TIMEOUT_SECONDS: Final = 300.0
#: One second between requests. The site is someone's free hosting bill.
MIN_REQUEST_INTERVAL_SECONDS: Final = 1.0

SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
MEDIA_ID_RE: Final = re.compile(r"^[0-9]{1,12}$")
STILL_PATH_RE: Final = re.compile(
    r"^/(?:i/c/([1-9][0-9]{0,3})x([1-9][0-9]{0,3})/)?"
    r"media/([0-9]{1,12})/([A-Za-z0-9][A-Za-z0-9._-]{0,239})$"
)
VIDEO_PATH_RE: Final = re.compile(
    r"^/media/([0-9]{1,12})/([A-Za-z0-9][A-Za-z0-9._-]{0,239}\.mp4)$", re.IGNORECASE
)
DOWNLOAD_PATH_RE: Final = re.compile(r"^/dl/(hd|4k)/([0-9]{1,12})/?$")
LISTING_PATH_RE: Final = re.compile(r"^/([a-z0-9][a-z0-9-]{0,119})/?$")

_STILL_EXTENSIONS: Final[tuple[str, ...]] = (".jpg", ".jpeg", ".png", ".webp")
_CHALLENGE_MARKERS: Final[tuple[str, ...]] = (
    "<title>just a moment",
    "challenge-error-text",
    "checking your browser",
)
_EMPTY_MARKERS: Final[tuple[str, ...]] = ("no wallpapers", "nothing found", "no results")


# -- text ----------------------------------------------------------------


def clean_text(value: str, maximum: int) -> str:
    """Collapse whitespace and control characters, then truncate.

    Applied to every string taken out of the remote's markup before it reaches
    a dataclass, a filename, or a label in the UI.
    """
    flattened = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character for character in value
    )
    return " ".join(flattened.split())[:maximum]


def normalise_query(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProviderError("validation", "search query contains control characters")
    normalised = " ".join(value.split())
    if not normalised or len(normalised.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ProviderError("validation", f"search query must be 1-{MAX_QUERY_BYTES} UTF-8 bytes")
    return normalised


#: The site publishes durations as ISO 8601 in its structured data, so a
#: 27-second loop reads `PT26.9S`. Correct, and not what anybody wants to see
#: on a card.
_DURATION_RE: Final = re.compile(
    r"^P(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)$"
)


def readable_duration(value: str) -> str:
    """`PT26.9S` as `27s`, or the original when it is not a duration.

    Rounded to whole seconds: the tenth is real but it is the length of a
    wallpaper loop, and nobody is timing it. Anything unparsed is passed
    through rather than dropped -- an odd-looking string is more use than a
    blank row.
    """
    match = _DURATION_RE.fullmatch(value.strip().upper()) if value else None
    if match is None or not any(match.groupdict().values()):
        return value
    parts = {name: float(found or 0) for name, found in match.groupdict().items()}
    total = round(parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"])
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def normalise_slug(value: str, *, label: str = "slug") -> str:
    if SLUG_RE.fullmatch(value) is None:
        raise ProviderError("validation", f"{label} is not a lowercase MotionBGS slug")
    return value


# -- URLs ----------------------------------------------------------------


def normalise_url(value: str, *, base: str | None = None) -> str:
    """Resolve ``value`` against MotionBGS and refuse anything that leaves it."""
    if not value or len(value) > MAX_URL_BYTES:
        raise ProviderError("invalid-url", "provider URL is empty or too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value) or "\\" in value:
        raise ProviderError("invalid-url", "provider URL contains unsafe characters")
    candidate = urljoin(base or ORIGIN + "/", value)
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as error:
        raise ProviderError("invalid-url", "provider URL contains an invalid port") from error
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProviderError("invalid-url", "only the exact MotionBGS HTTPS origin is accepted")
    if not parsed.path.startswith("/") or "//" in parsed.path:
        raise ProviderError("invalid-url", "provider URL path is malformed")
    # Decode the separators a traversal would hide behind before looking for
    # one: `urlsplit` leaves `%2e%2e%2f` alone, and so would a naive check.
    decoded = re.sub("%2f", "/", parsed.path, flags=re.IGNORECASE)
    decoded = re.sub("%5c", "\\\\", decoded, flags=re.IGNORECASE)
    decoded = re.sub("%2e", ".", decoded, flags=re.IGNORECASE)
    if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
        raise ProviderError("invalid-url", "provider URL contains a traversal path")
    # Port 443 is the default, so canonicalise it away: two spellings of the
    # same URL must compare equal when a redirect is checked against a route.
    return urlunsplit(("https", HOST, parsed.path, parsed.query, ""))


def site_path(value: str) -> str:
    path = urlsplit(normalise_url(value)).path.rstrip("/")
    return path or "/"


def still_url(value: str) -> str:
    """A MotionBGS thumbnail URL, or ``""`` if ``value`` is not one."""
    if not value:
        return ""
    try:
        normalised = normalise_url(value)
    except ProviderError:
        return ""
    parsed = urlsplit(normalised)
    if parsed.query:
        return ""
    match = STILL_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return ""
    width, height, _media_id, filename = match.groups()
    if width is not None and (int(width) > 4096 or int(height) > 4096):
        return ""
    if not filename.lower().endswith(_STILL_EXTENSIONS):
        return ""
    return normalised


def video_url(value: str) -> str:
    """A MotionBGS `.mp4` URL, or ``""`` if ``value`` is not one."""
    if not value:
        return ""
    try:
        normalised = normalise_url(value)
    except ProviderError:
        return ""
    parsed = urlsplit(normalised)
    if parsed.query or VIDEO_PATH_RE.fullmatch(parsed.path) is None:
        return ""
    return normalised


def browse_url(mode: str, query: str, genre: str, page: int) -> str:
    if mode == "search":
        return f"{ORIGIN}/search?q={quote_plus(query)}"
    if mode == "latest":
        return ORIGIN + "/" + ("" if page == 1 else f"{page}/")
    if mode == "genre":
        return f"{ORIGIN}/tag:{genre}/" + ("" if page == 1 else f"{page}/")
    if mode == "4k":
        return f"{ORIGIN}/4k/" + ("" if page == 1 else f"{page}/")
    return f"{ORIGIN}/hd/"


def _canonical_search_slug(query: str) -> str | None:
    candidate = query.lower().replace(" ", "-")
    return candidate if SLUG_RE.fullmatch(candidate) is not None else None


def listing_route_matches(mode: str, query: str, genre: str, page: int, value: str) -> bool:
    """Did we land on the listing we asked for?

    A search often redirects to the equivalent tag page, which is the same
    results by another name and is allowed. Anything else means a redirect
    moved us into a different catalogue and the results are not ours.
    """
    normalised = normalise_url(value)
    parsed = urlsplit(normalised)
    actual = parsed.path.rstrip("/") or "/"
    if mode == "search":
        canonical = _canonical_search_slug(query)
        return normalised == browse_url(mode, query, genre, page) or (
            parsed.query == "" and canonical is not None and actual == f"/tag:{canonical}"
        )
    return parsed.query == "" and actual == site_path(browse_url(mode, query, genre, page))


# -- bounded HTML --------------------------------------------------------


@dataclass(slots=True)
class _Card:
    attrs: dict[str, str]
    title_parts: list[str] = field(default_factory=list)
    quality_parts: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    image: str = ""


@dataclass(slots=True)
class _Anchor:
    attrs: dict[str, str]
    text_parts: list[str] = field(default_factory=list)


class _BoundedParser(HTMLParser):
    """`HTMLParser` with ceilings, so a hostile page cannot outlast us."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags_seen = 0

    def _attributes(self, attributes: list[tuple[str, str | None]]) -> dict[str, str]:
        self.tags_seen += 1
        if self.tags_seen > MAX_HTML_TAGS:
            raise ProviderError("site-markup", "MotionBGS page exceeded the bounded tag limit")
        if len(attributes) > MAX_ATTRIBUTES_PER_TAG:
            raise ProviderError("site-markup", "MotionBGS tag exceeded the bounded attribute limit")
        normalised: dict[str, str] = {}
        for key, value in attributes:
            text = value or ""
            if len(text.encode("utf-8", "replace")) > MAX_ATTRIBUTE_BYTES:
                continue
            lower = key.lower()
            if lower not in normalised:
                normalised[lower] = text
        return normalised


class _ListingParser(_BoundedParser):
    def __init__(self) -> None:
        super().__init__()
        self.card: _Card | None = None
        self.card_depth = 0
        self.span_class = ""
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.cards: list[_Card] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attributes(attrs)
        tag = tag.lower()
        if tag == "title":
            self.title_depth += 1
        if tag == "link" and len(self.links) < 64:
            self.links.append(attributes)
        if tag == "a":
            if self.card is None:
                self.card = _Card(attrs=attributes)
                self.card_depth = 1
            else:
                self.card_depth += 1
            return
        if self.card is None:
            return
        if tag == "span":
            classes = set(attributes.get("class", "").split())
            if "ttl" in classes:
                self.span_class = "ttl"
            elif "frm" in classes:
                self.span_class = "frm"
        elif tag == "img" and not self.card.image:
            for key in ("data-src", "data-lazy-src", "data-original", "src"):
                candidate = still_url(attributes.get(key, ""))
                if candidate:
                    self.card.image = candidate
                    break
        elif tag == "source" and not self.card.image:
            self.card.image = _first_still_in_srcset(attributes)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if self.card is None:
            return
        if tag == "span":
            self.span_class = ""
        elif tag == "a":
            self.card_depth -= 1
            if self.card_depth <= 0:
                self.cards.append(self.card)
                self.card = None
                self.card_depth = 0
                self.span_class = ""

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.card is None:
            return
        self.card.text_parts.append(data)
        if self.span_class == "ttl":
            self.card.title_parts.append(data)
        elif self.span_class == "frm":
            self.card.quality_parts.append(data)


def _first_still_in_srcset(attributes: dict[str, str]) -> str:
    for key in ("data-srcset", "srcset", "data-src", "src"):
        for entry in attributes.get(key, "").split(","):
            candidate = still_url(entry.strip().split(" ", 1)[0])
            if candidate:
                return candidate
    return ""


class _DetailParser(_BoundedParser):
    def __init__(self) -> None:
        super().__init__()
        self.metas: dict[str, str] = {}
        self.sources: list[dict[str, str]] = []
        self.anchors: list[_Anchor] = []
        self.anchor: _Anchor | None = None
        self.anchor_depth = 0
        self.title_depth = 0
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attributes(attrs)
        tag = tag.lower()
        if tag == "title":
            self.title_depth += 1
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key and key not in self.metas:
                self.metas[key] = attributes.get("content", "")
        elif tag == "source" and len(self.sources) < 64:
            self.sources.append(attributes)
        elif tag == "a":
            if self.anchor is None:
                self.anchor = _Anchor(attrs=attributes)
                self.anchor_depth = 1
            else:
                self.anchor_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if self.anchor is not None and tag == "a":
            self.anchor_depth -= 1
            if self.anchor_depth <= 0:
                self.anchors.append(self.anchor)
                self.anchor = None
                self.anchor_depth = 0

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.anchor is not None:
            self.anchor.text_parts.append(data)


def _refuse_challenge(markup: str) -> None:
    prefix = markup[:16_384].lower()
    if any(marker in prefix for marker in _CHALLENGE_MARKERS):
        raise ProviderError(
            "challenge",
            "MotionBGS returned an anti-bot challenge; no bypass was attempted",
        )


def _feed(parser: _BoundedParser, markup: str, what: str) -> None:
    try:
        parser.feed(markup)
        parser.close()
    except ProviderError:
        raise
    except Exception as error:
        raise ProviderError(
            "site-markup", f"could not parse bounded MotionBGS {what}: {error}"
        ) from error


def validate_html(content_type: str, body: bytes) -> str:
    """Insist the bytes are HTML in both the header and the body."""
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ProviderError(
            "content-type", f"expected HTML but received {content_type or 'unknown'}"
        )
    signature = body[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if not signature.startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ProviderError("content-type", "HTML MIME type did not match the response signature")
    if b"\x00" in body:
        raise ProviderError("content-type", "HTML response contains NUL bytes")
    return body.decode("utf-8", "replace")


# -- parsed documents ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadOption:
    """One quality a detail page offers."""

    quality: str
    media_id: str
    label: str
    resolution: str
    advertised_size_mb: float
    url: str


@dataclass(frozen=True, slots=True)
class MotionBgsDetail:
    slug: str
    media_id: str
    title: str
    page_url: str
    preview_url: str
    poster_url: str
    duration: str
    downloads: tuple[DownloadOption, ...]

    def option(self, quality: str) -> DownloadOption:
        """The requested quality, or the best available when unspecified."""
        if quality:
            for candidate in self.downloads:
                if candidate.quality == quality:
                    return candidate
            raise ProviderError("validation", f"MotionBGS does not offer {quality} for {self.slug}")
        # 4k first: a video wallpaper is worth the bytes, and the caller can
        # always ask for hd by name.
        for preferred in ("4k", "hd"):
            for candidate in self.downloads:
                if candidate.quality == preferred:
                    return candidate
        raise ProviderError("validation", f"MotionBGS offers no download for {self.slug}")


@dataclass(frozen=True, slots=True)
class ListingPage:
    items: tuple[WallpaperCandidate, ...]
    page: int
    has_previous: bool
    has_next: bool
    total_hint: int


def parse_listing(
    markup: str, *, mode: str, query: str, genre: str, page: int, source_url: str, limit: int
) -> ListingPage:
    _refuse_challenge(markup)
    parser = _ListingParser()
    _feed(parser, markup, "listing")

    items: list[WallpaperCandidate] = []
    seen: set[str] = set()
    for card in parser.cards:
        candidate = _card_to_candidate(card, seen)
        if candidate is None:
            continue
        items.append(candidate)
        seen.add(candidate.identifier)
        if len(items) >= limit:
            break

    if not items:
        prefix = markup[:16_384].lower()
        if not any(marker in prefix for marker in _EMPTY_MARKERS):
            raise ProviderError(
                "site-markup",
                "MotionBGS returned no recognisable wallpaper cards; "
                "its public markup may have changed",
            )

    if not listing_route_matches(mode, query, genre, page, source_url):
        raise ProviderError("redirects", "MotionBGS redirected into a different catalog route")

    pageable = mode in {"latest", "genre", "4k"}
    has_previous = False
    has_next = False
    if pageable:
        for attributes in parser.links[:32]:
            relations = {token.lower() for token in attributes.get("rel", "").split()}
            try:
                candidate_url = normalise_url(attributes.get("href", ""))
            except ProviderError:
                continue
            if (
                "prev" in relations
                and page > 1
                and listing_route_matches(mode, query, genre, page - 1, candidate_url)
            ):
                has_previous = True
            if (
                "next" in relations
                and page < MAX_PAGE
                and listing_route_matches(mode, query, genre, page + 1, candidate_url)
            ):
                has_next = True

    title = clean_text(" ".join(parser.title_parts), 300)
    total_match = re.match(r"^([0-9,]+)\+", title)
    total_hint = min(int(total_match.group(1).replace(",", "")), 2**53 - 1) if total_match else 0
    return ListingPage(
        items=tuple(items),
        page=page,
        has_previous=has_previous,
        has_next=has_next,
        total_hint=total_hint,
    )


def _card_to_candidate(card: _Card, seen: set[str]) -> WallpaperCandidate | None:
    title_attribute = card.attrs.get("title", "")
    title = clean_text(" ".join(card.title_parts), 240)
    if not title and "live wallpaper" not in title_attribute.lower():
        return None
    try:
        href = normalise_url(card.attrs.get("href", ""))
    except ProviderError:
        return None
    match = LISTING_PATH_RE.fullmatch(urlsplit(href).path)
    if match is None or match.group(1) in seen:
        return None
    slug = match.group(1)
    if not title:
        title = clean_text(
            re.sub(r"live\s+wallpaper", "", title_attribute, flags=re.IGNORECASE), 240
        )
    quality = clean_text(" ".join(card.quality_parts), 20).upper()
    if not quality:
        quality_match = re.search(r"\b(4K|HD)\b", title_attribute, flags=re.IGNORECASE)
        quality = quality_match.group(1).upper() if quality_match else ""
    return WallpaperCandidate(
        provider=MotionBgs.name,
        identifier=slug,
        title=title or slug,
        kind=Kind.VIDEO,
        page_url=f"{ORIGIN}/{slug}",
        thumbnail_url=card.image,
        resolution=quality,
        variant=quality.lower() if quality.lower() in {"hd", "4k"} else "",
    )


def parse_detail(markup: str, slug: str) -> MotionBgsDetail:
    _refuse_challenge(markup)
    parser = _DetailParser()
    _feed(parser, markup, "details")

    title = clean_text(parser.metas.get("og:title") or " ".join(parser.title_parts), 240)
    title = re.sub(r"\s+live\s+wallpaper\s*$", "", title, flags=re.IGNORECASE)
    poster = still_url(parser.metas.get("og:image", ""))
    preview = video_url(parser.metas.get("og:video", ""))
    if not preview:
        for attributes in parser.sources:
            media_type = attributes.get("type", "").split(";", 1)[0].lower()
            candidate = video_url(attributes.get("src", ""))
            if candidate and media_type in {"", "video/mp4"}:
                preview = candidate
                break

    options: dict[str, DownloadOption] = {}
    media_id = ""
    for anchor in parser.anchors:
        try:
            normalised = normalise_url(anchor.attrs.get("href", ""))
        except ProviderError:
            continue
        match = DOWNLOAD_PATH_RE.fullmatch(urlsplit(normalised).path)
        if match is None:
            continue
        quality, identifier = match.groups()
        body = clean_text(" ".join(anchor.text_parts), 240)
        resolution_match = re.search(r"([0-9]{2,5})\s*[xX]\s*([0-9]{2,5})", body)
        size_match = re.search(r"\(([0-9]+(?:\.[0-9]+)?)\s*MB\)", body, flags=re.IGNORECASE)
        options[quality] = DownloadOption(
            quality=quality,
            media_id=identifier,
            label=quality.upper(),
            resolution=(
                f"{resolution_match.group(1)}x{resolution_match.group(2)}"
                if resolution_match
                else ""
            ),
            advertised_size_mb=float(size_match.group(1)) if size_match else 0.0,
            url=f"{ORIGIN}/dl/{quality}/{identifier}/",
        )
        media_id = media_id or identifier

    if not options or not media_id:
        raise ProviderError(
            "site-markup",
            "MotionBGS did not expose recognisable HD/4K download links; "
            "its public markup may have changed",
        )
    duration_match = re.search(r'"duration"\s*:\s*"(PT[^"\\]{1,40})"', markup)
    return MotionBgsDetail(
        slug=slug,
        media_id=media_id,
        title=title or slug,
        page_url=f"{ORIGIN}/{slug}",
        preview_url=preview,
        poster_url=poster,
        duration=duration_match.group(1) if duration_match else "",
        downloads=tuple(options[key] for key in sorted(options)),
    )


# -- downloaded bytes ----------------------------------------------------


def validate_download_route(value: str, quality: str, media_id: str) -> str:
    """Bind a finished transfer to the detail record that authorised it."""
    normalised = normalise_url(value)
    parsed = urlsplit(normalised)
    if parsed.query:
        raise ProviderError("redirects", "download resolved to a URL with unexpected query data")
    download_match = DOWNLOAD_PATH_RE.fullmatch(parsed.path)
    if download_match is not None:
        if download_match.groups() == (quality, media_id):
            return normalised
        raise ProviderError("redirects", "download resolved to a different MotionBGS media id")
    video_match = VIDEO_PATH_RE.fullmatch(parsed.path)
    if video_match is not None and video_match.group(1) == media_id:
        return normalised
    raise ProviderError(
        "redirects", "download resolved outside the authorised MotionBGS media route"
    )


def validate_mp4(path: Path, content_type: str) -> tuple[int, str]:
    """Confirm the bytes are an ISO-BMFF file, and hash them.

    The MIME type alone is the remote's opinion. The `ftyp` box is the file's.
    """
    if content_type not in {"video/mp4", "application/octet-stream"}:
        raise ProviderError(
            "content-type", f"expected MP4 but received {content_type or 'unknown'}"
        )
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            prefix = stream.read(64)
            digest = hashlib.sha256()
            digest.update(prefix)
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise ProviderError("local-io", f"could not read the download: {error}") from error
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        raise ProviderError(
            "content-type", "download MIME type did not match an ISO-BMFF ftyp signature"
        )
    box_size = int.from_bytes(prefix[:4], "big")
    if box_size != 0 and (box_size < 12 or box_size > size):
        raise ProviderError("content-type", "MP4 ftyp box has an invalid declared size")
    return size, digest.hexdigest()


# -- the provider --------------------------------------------------------


class MotionBgs:
    """Video wallpapers from motionbgs.com."""

    name: str = "motionbgs"
    title: str = "MotionBGS"
    media_kind: Kind = Kind.VIDEO

    def __init__(
        self,
        client: http.Client | None = None,
        *,
        rate_limiter: http.RateLimiter | None = None,
        max_download_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> None:
        self._client: http.Client = client if client is not None else http.UrllibClient()
        self._rate = (
            rate_limiter
            if rate_limiter is not None
            else http.RateLimiter(MIN_REQUEST_INTERVAL_SECONDS)
        )
        self._max_download_bytes = max_download_bytes
        self._listings: TtlCache[ListingPage] = TtlCache(max_entries=8)
        self._details: TtlCache[MotionBgsDetail] = TtlCache(max_entries=48)

    # -- transport -------------------------------------------------------

    def _get_html(self, url: str, purpose: str) -> tuple[str, str]:
        """Fetch ``url``, following same-origin redirects by hand."""
        current = normalise_url(url)
        for _hop in range(MAX_REDIRECTS + 1):
            self._rate.wait()
            response = self._client.fetch(
                http.Request(
                    url=current,
                    accept="text/html,application/xhtml+xml;q=0.9",
                    timeout=HTML_TIMEOUT_SECONDS,
                    max_bytes=MAX_HTML_BYTES,
                )
            )
            if response.is_redirect:
                if not response.location:
                    raise ProviderError("redirects", "redirect did not include a Location header")
                # Re-anchoring on the *current* URL and re-validating is what
                # makes a cross-origin redirect fail closed.
                current = normalise_url(response.location, base=current)
                continue
            _refuse_error_status(response.status, purpose)
            return validate_html(response.content_type, response.body), current
        raise ProviderError("redirects", "MotionBGS returned too many redirects")

    # -- search ----------------------------------------------------------

    def search(self, query: SearchQuery) -> SearchResult:
        mode, text, genre, page, limit = _plan_search(query)
        key = f"{mode}\0{text}\0{genre}\0{page}\0{limit}"
        source_url = browse_url(mode, text, genre, page)
        cached = self._listings.get(key)
        if cached is not None:
            return _to_result(cached, source_url, cached_flag=True)
        markup, effective = self._get_html(source_url, "listing")
        listing = parse_listing(
            markup,
            mode=mode,
            query=text,
            genre=genre,
            page=page,
            source_url=effective,
            limit=limit,
        )
        self._listings.put(key, listing)
        return _to_result(listing, source_url, cached_flag=False)

    def detail(self, slug: str) -> MotionBgsDetail:
        slug = normalise_slug(slug)
        cached = self._details.get(slug)
        if cached is not None:
            return cached
        source_url = f"{ORIGIN}/{slug}"
        markup, effective = self._get_html(source_url, "details")
        if site_path(effective) != site_path(source_url):
            raise ProviderError("redirects", "detail request redirected to another page")
        detail = parse_detail(markup, slug)
        self._details.put(slug, detail)
        return detail

    def describe(self, candidate: WallpaperCandidate) -> CandidateDetail:
        """The detail page, arranged for a window.

        MotionBGS knows less than Wallhaven and offers something Wallhaven does
        not: two download qualities. Those become `variants`, so the dialog can
        let somebody take the HD file when they do not want 40 MB of 4K.

        Sizes here are the site's own advertised megabytes rather than a byte
        count, because nothing has been fetched yet -- it is a claim on a page,
        and is labelled as approximate for that reason.
        """
        detail = self.detail(candidate.identifier)
        facts = [Fact("Title", detail.title), Fact("Duration", readable_duration(detail.duration))]
        for option in detail.downloads:
            size = (
                human_bytes(int(option.advertised_size_mb * 1024 * 1024))
                if option.advertised_size_mb > 0
                else ""
            )
            value = ", ".join(
                part for part in (option.resolution, f"~{size}" if size else "") if part
            )
            facts.append(Fact(option.quality.upper(), value or option.label))
        return CandidateDetail(
            candidate=candidate,
            # The poster frame: a still of the video, which is the only large
            # preview a detail page offers short of the video itself.
            preview_url=detail.poster_url or candidate.thumbnail_url,
            facts=tuple(fact for fact in facts if fact.value),
            variants=tuple(option.quality for option in detail.downloads),
        )

    # -- download --------------------------------------------------------

    def download(
        self, candidate: WallpaperCandidate, root: Path, *, variant: str = ""
    ) -> DownloadResult:
        slug = normalise_slug(candidate.identifier)
        quality = (variant or candidate.variant or "").lower()
        if quality and quality not in {"hd", "4k"}:
            raise ProviderError("validation", "MotionBGS quality must be hd or 4k")
        detail = self.detail(slug)
        option = detail.option(quality)
        if MEDIA_ID_RE.fullmatch(option.media_id) is None:
            raise ProviderError("validation", "selected MotionBGS quality has no media id")

        directory, marker = download_module.managed_directory(
            root, download_module.MOTIONBGS_LOCATION
        )
        transfer = self._stream(option, directory)
        with transfer:
            if transfer.path is None:
                raise ProviderError("transport", "download produced no body")
            effective = validate_download_route(transfer.url, option.quality, option.media_id)
            size, digest = validate_mp4(transfer.path, transfer.content_type)
            if size != transfer.size:
                raise ProviderError(
                    "size-mismatch", "validated MP4 size did not match the transfer record"
                )
            destination = download_module.unique_destination(
                directory,
                f"{slug}.{option.quality}",
                ".mp4",
                download_module.MOTIONBGS_LOCATION.sidecar_suffix,
            )
            downloaded_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = download_module.encode_sidecar(
                {
                    "schema": 1,
                    "plugin": "goober/wall-in-one",
                    "provider": "MotionBGS",
                    # The slug, under the name Wallhaven's sidecar already uses
                    # for the same thing, so `library.owned` has one key to
                    # look for rather than two. Sidecars written before this
                    # carry no id, which is why `source_page` remains a key
                    # instead of being replaced by it.
                    "id": detail.slug,
                    "path": str(destination),
                    "title": detail.title,
                    "source_page": detail.page_url,
                    "download_url": effective,
                    "quality": option.quality,
                    "content_type": transfer.content_type,
                    "bytes": size,
                    "sha256": digest,
                    "downloaded_at": downloaded_at,
                }
            )
            installed, sidecar = download_module.install(
                transfer.path,
                destination,
                download_module.MOTIONBGS_LOCATION.sidecar_suffix,
                payload,
            )
        return DownloadResult(
            provider=self.name,
            identifier=slug,
            path=installed,
            sidecar=sidecar,
            marker=marker,
            kind=Kind.VIDEO,
            size=size,
            source_url=detail.page_url,
            download_url=effective,
            sha256=digest,
            downloaded_at=downloaded_at,
        )

    def _stream(self, option: DownloadOption, directory: Path) -> http.Transfer:
        """Follow the `/dl/` redirect chain to the media file, staging bytes."""
        current = normalise_url(option.url)
        for _hop in range(MAX_REDIRECTS + 1):
            self._rate.wait()
            transfer = self._client.download(
                http.Request(
                    url=current,
                    accept="video/mp4,application/octet-stream;q=0.8",
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                    max_bytes=self._max_download_bytes,
                ),
                directory,
            )
            if transfer.is_redirect:
                transfer.discard()
                if not transfer.location:
                    raise ProviderError("redirects", "redirect did not include a Location header")
                current = normalise_url(transfer.location, base=current)
                continue
            try:
                _refuse_error_status(transfer.status, "download")
            except ProviderError:
                transfer.discard()
                raise
            # The client reports the URL it was given; the route check below is
            # against the URL we resolved to, which is the authorising one.
            return http.Transfer(
                url=current,
                status=transfer.status,
                content_type=transfer.content_type,
                size=transfer.size,
                path=transfer.path,
                location=transfer.location,
            )
        raise ProviderError("redirects", "MotionBGS returned too many redirects")

    def clear_cache(self) -> None:
        self._listings.clear()
        self._details.clear()


def _refuse_error_status(status: int, purpose: str) -> None:
    if status in {403, 429}:
        raise ProviderError(
            "challenge",
            f"MotionBGS refused the public request (HTTP {status}); no bypass was attempted",
        )
    if not 200 <= status < 300:
        raise ProviderError("http", f"MotionBGS returned HTTP {status} for {purpose}")


def _plan_search(query: SearchQuery) -> tuple[str, str, str, int, int]:
    mode = query.option("mode", "search" if query.text else "latest")
    if mode not in MODES:
        raise ProviderError("validation", "browse mode must be search, latest, genre, 4k, or hd")
    page = query.page
    if page < 1 or page > MAX_PAGE:
        raise ProviderError("validation", f"page must be 1-{MAX_PAGE}")
    text = ""
    genre = ""
    if mode == "search":
        text = normalise_query(query.text)
        if page != 1:
            raise ProviderError("validation", "MotionBGS text search does not expose pagination")
    elif query.text:
        raise ProviderError("validation", "a query is valid only in search mode")
    if mode == "genre":
        genre = normalise_slug(query.option("genre"), label="genre")
    elif query.option("genre"):
        raise ProviderError("validation", "a genre is valid only in genre mode")
    if mode == "hd" and page != 1:
        raise ProviderError("validation", "MotionBGS HD browsing is intentionally first-page-only")
    unknown = set(query.options) - {"mode", "genre"}
    if unknown:
        raise ProviderError(
            "invalid-request", f"unsupported MotionBGS options: {', '.join(sorted(unknown))}"
        )
    return mode, text, genre, page, MAX_RESULTS


def _to_result(listing: ListingPage, source_url: str, *, cached_flag: bool) -> SearchResult:
    return SearchResult(
        provider=MotionBgs.name,
        query_url=source_url,
        items=listing.items,
        page=listing.page,
        has_previous=listing.has_previous,
        has_next=listing.has_next,
        total_hint=listing.total_hint,
        cached=cached_flag,
    )
