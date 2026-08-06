"""Fetching wallpapers from other people's servers.

Lifted from the previous implementation's 5,493-line helper script with the
request/response-file RPC transport removed: providers are ordinary objects the
app calls directly, and every network call goes through
`wall_in_one.providers.http.Client`, which is the only seam a test has to
replace to run the whole package offline.
"""

from __future__ import annotations

from wall_in_one.providers.base import (
    DownloadResult,
    Provider,
    ProviderError,
    SearchQuery,
    SearchResult,
    WallpaperCandidate,
)
from wall_in_one.providers.motionbgs import MotionBgs
from wall_in_one.providers.registry import ProviderInfo, build, build_all, describe, names
from wall_in_one.providers.wallhaven import Wallhaven, WallhavenFilters

__all__ = [
    "DownloadResult",
    "MotionBgs",
    "Provider",
    "ProviderError",
    "ProviderInfo",
    "SearchQuery",
    "SearchResult",
    "Wallhaven",
    "WallhavenFilters",
    "WallpaperCandidate",
    "build",
    "build_all",
    "describe",
    "names",
]
