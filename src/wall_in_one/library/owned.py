"""What the library already holds, keyed by where it came from.

The browse dialog offers every result as though it were new. It is not: a user
who searches "mountain" twice a week is looking at wallpapers they downloaded
last time, with no way to tell which. The library knows -- the download sidecar
records the provider and the source page -- and nobody has ever asked it.

This is the asking. It reads the sidecars the providers wrote and answers one
question: *have we got this already, and where is it?*

Two keys, because the two providers record different things. Wallhaven writes
an `id`, so `(provider, identifier)` works. MotionBGS writes no id at all --
its identifier is a slug that only appears inside `source_page` -- so the page
URL is the key that both providers can be matched on. Both are indexed and
either will answer, which also means sidecars written before this module
existed still match.

Deliberately a snapshot rather than a live view. It is built once when the
browse dialog opens and thrown away when it closes: a search is a handful of
seconds and the library does not change underneath it except by this dialog's
own downloads, which report their own path anyway.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one.providers.base import WallpaperCandidate

#: The sidecar suffixes that record a download. `library.scan` knows a third,
#: the pairing sidecar, which says nothing about where a file came from.
SIDECAR_SUFFIXES: Final[tuple[str, ...]] = (".wallhaven.json", ".motionbgs.json")

#: A sidecar is a few hundred bytes. This is a ceiling on damage, not a budget:
#: something that large is not a sidecar and should not be parsed as one.
MAX_SIDECAR_BYTES: Final = 64 * 1024

#: Stop walking a root that turns out to be enormous. Six hundred wallpapers is
#: the size this was written for; a user who points a root at their home
#: directory should get a slow answer rather than no answer.
MAX_SIDECARS: Final = 20_000


@dataclass(frozen=True, slots=True)
class Origin:
    """Where a file in the library came from."""

    #: Casefolded, because the sidecar says "Wallhaven" and the provider calls
    #: itself "wallhaven". Neither is wrong and neither is going to change.
    provider: str
    identifier: str


class Index:
    """Which of a provider's results the library already holds.

    Empty is the honest answer to "we could not read the sidecars", and it is
    also the safe one: a browser that fails to notice a duplicate offers a
    download the user may not need, which is a smaller harm than one that
    wrongly claims a wallpaper is already there and hides it.
    """

    def __init__(
        self,
        by_origin: Mapping[Origin, Path] | None = None,
        by_page: Mapping[str, Path] | None = None,
    ) -> None:
        self._by_origin: dict[Origin, Path] = dict(by_origin or {})
        self._by_page: dict[str, Path] = dict(by_page or {})

    def __len__(self) -> int:
        """How many downloads are indexed, counting each file once."""
        return len({*self._by_origin.values(), *self._by_page.values()})

    def path_for(self, candidate: WallpaperCandidate) -> Path | None:
        """Where ``candidate`` already lives, or ``None``."""
        origin = Origin(provider=candidate.provider.casefold(), identifier=candidate.identifier)
        found = self._by_origin.get(origin)
        if found is not None:
            return found
        if candidate.page_url:
            return self._by_page.get(candidate.page_url)
        return None

    def holds(self, candidate: WallpaperCandidate) -> bool:
        return self.path_for(candidate) is not None

    def add(self, candidate: WallpaperCandidate, path: Path) -> None:
        """Record a download that just finished.

        So a card updates the moment its download lands, without re-walking
        every root to learn one thing this process already knows.
        """
        origin = Origin(provider=candidate.provider.casefold(), identifier=candidate.identifier)
        self._by_origin[origin] = path
        if candidate.page_url:
            self._by_page[candidate.page_url] = path


def _entry(sidecar: Path) -> tuple[Origin | None, str, Path] | None:
    """Read one sidecar into an origin, a page URL, and the file it describes.

    Returns ``None`` for anything unreadable. A malformed sidecar is a fact
    about one file, not a reason to abandon the index.
    """
    media = sidecar.with_name(sidecar.name[: -len(_suffix_of(sidecar))])
    # The file having gone is the interesting case: a sidecar left behind by a
    # deletion must not make the browser claim we still hold the wallpaper.
    if not media.is_file():
        return None
    try:
        if sidecar.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        raw = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    provider = payload.get("provider")
    identifier = payload.get("id")
    page = payload.get("source_page") or payload.get("page_url")
    origin = (
        Origin(provider=provider.casefold(), identifier=identifier)
        if isinstance(provider, str) and isinstance(identifier, str) and provider and identifier
        else None
    )
    return origin, page if isinstance(page, str) else "", media


def _suffix_of(sidecar: Path) -> str:
    for suffix in SIDECAR_SUFFIXES:
        if sidecar.name.endswith(suffix):
            return suffix
    return ""


def _sidecars(roots: Iterable[Path]) -> Iterable[Path]:
    """Every download sidecar under ``roots``, symlinked directories skipped.

    Bounded by `MAX_SIDECARS`, and deduplicated by path so that overlapping
    roots -- which the settings allow -- do not index the same file twice.
    """
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in SIDECAR_SUFFIXES:
            for found in root.rglob(f"*{suffix}"):
                if len(seen) >= MAX_SIDECARS:
                    return
                if found in seen or found.is_symlink() or not found.is_file():
                    continue
                seen.add(found)
                yield found


def read(roots: Sequence[Path]) -> Index:
    """Index every download recorded under ``roots``."""
    by_origin: dict[Origin, Path] = {}
    by_page: dict[str, Path] = {}
    for sidecar in _sidecars(roots):
        entry = _entry(sidecar)
        if entry is None:
            continue
        origin, page, media = entry
        if origin is not None:
            by_origin[origin] = media
        if page:
            by_page[page] = media
    return Index(by_origin, by_page)
