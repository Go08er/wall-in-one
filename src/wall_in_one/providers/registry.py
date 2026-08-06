"""Which providers exist, and which of them can actually do anything.

"Available" is not a boolean in practice. Wallhaven works without an API key
but silently cannot reach NSFW results with one missing, and a UI that offers
the filter anyway produces an empty grid and a confused user. So a provider
reports its *limitations* alongside its usability, and the UI can grey out
precisely the controls that would not work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.library.model import Kind
from wall_in_one.providers import http
from wall_in_one.providers.base import Provider, ProviderError
from wall_in_one.providers.motionbgs import MotionBgs
from wall_in_one.providers.wallhaven import Wallhaven, normalise_api_key

#: Read when no key is passed explicitly.
API_KEY_VARIABLE: Final = "WALLHAVEN_API_KEY"

#: Fallback to a file, so the key need not sit in the environment of every
#: process the user starts. One line, nothing else.
API_KEY_FILENAME: Final = "wallhaven-api-key"

#: A key file is small; anything larger is not one.
MAX_KEY_FILE_BYTES: Final = 4096


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """What the UI needs to decide whether to show a provider at all."""

    name: str
    title: str
    media_kind: Kind
    usable: bool
    #: Empty when everything works. Each entry names something the user must
    #: supply before a particular feature is reachable.
    limitations: tuple[str, ...] = ()


def wallhaven_api_key(explicit: str = "") -> str:
    """The key from, in order: the caller, the environment, the config file."""
    if explicit:
        return normalise_api_key(explicit)
    from_environment = os.environ.get(API_KEY_VARIABLE, "")
    if from_environment:
        return normalise_api_key(from_environment)
    return _key_from_file(paths.app_config_dir() / API_KEY_FILENAME)


def _key_from_file(path: Path) -> str:
    """Read a key file, tolerating its absence and refusing its excesses."""
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        if path.stat().st_size > MAX_KEY_FILE_BYTES:
            return ""
        raw = path.read_bytes()
    except OSError:
        return ""
    try:
        first_line = raw.decode("utf-8").splitlines()[0] if raw.strip() else ""
    except (UnicodeDecodeError, IndexError):
        return ""
    try:
        return normalise_api_key(first_line)
    except ProviderError:
        # A malformed key file should not stop the app starting; Wallhaven
        # simply runs unauthenticated and says so through `limitations`.
        return ""


def describe(*, api_key: str = "") -> tuple[ProviderInfo, ...]:
    """Every provider and what it can do right now. Never raises."""
    try:
        key = wallhaven_api_key(api_key)
    except ProviderError:
        key = ""
    return (
        ProviderInfo(
            name=MotionBgs.name,
            title=MotionBgs.title,
            media_kind=MotionBgs.media_kind,
            usable=True,
        ),
        ProviderInfo(
            name=Wallhaven.name,
            title=Wallhaven.title,
            media_kind=Wallhaven.media_kind,
            usable=True,
            limitations=() if key else ("NSFW results need a Wallhaven API key",),
        ),
    )


def names() -> tuple[str, ...]:
    return (MotionBgs.name, Wallhaven.name)


def build(name: str, *, client: http.Client | None = None, api_key: str = "") -> Provider:
    """Construct one provider by name, sharing ``client`` if one is given."""
    if name == MotionBgs.name:
        return MotionBgs(client)
    if name == Wallhaven.name:
        return Wallhaven(client, api_key=wallhaven_api_key(api_key))
    raise ProviderError("unknown-provider", f"no such provider: {name!r}")


def build_all(*, client: http.Client | None = None, api_key: str = "") -> tuple[Provider, ...]:
    """Every provider, over one shared transport."""
    shared = client if client is not None else http.UrllibClient()
    return tuple(build(name, client=shared, api_key=api_key) for name in names())
