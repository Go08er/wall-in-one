"""Wrapper around the ``noctalia`` CLI.

Two surfaces are used:

``noctalia theme <image> [options]``
    A pure function of its arguments -- no event loop, no config, no running
    shell required (``noctalia/src/theme/cli.h``). Deterministic, ~0.25s.

``noctalia msg <command> [args]``
    IPC to the running shell. Requires Noctalia to be up.

Both write only their payload to stdout and send logs to stderr, so no output
filtering is needed.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from wall_in_one.theme.palette import Mode, PaletteError, PalettePair

#: Generation strategies, from `noctalia/src/theme/scheme.h`. The first five are
#: Material Design 3; the rest are custom HSL-space generators with deliberately
#: different aesthetics.
MATERIAL_SCHEMES: Final[tuple[str, ...]] = (
    "m3-tonal-spot",
    "m3-content",
    "m3-fruit-salad",
    "m3-rainbow",
    "m3-monochrome",
)

CUSTOM_SCHEMES: Final[tuple[str, ...]] = (
    "vibrant",
    "faithful",
    "soft",
    "dysfunctional",
    "muted",
)

ALL_SCHEMES: Final[tuple[str, ...]] = MATERIAL_SCHEMES + CUSTOM_SCHEMES

DEFAULT_SCHEME: Final = "m3-tonal-spot"

PaletteSource = Literal["builtin", "wallpaper", "community", "custom"]

#: Generous. Palette generation measures ~0.25s; this only bounds a hang.
GENERATE_TIMEOUT: Final = 30.0
#: IPC round-trips are sub-millisecond when the shell is up, and fail fast when
#: it is not.
MESSAGE_TIMEOUT: Final = 10.0


class NoctaliaError(Exception):
    """A ``noctalia`` invocation failed or was unavailable."""


class NoctaliaUnavailableError(NoctaliaError):
    """The ``noctalia`` binary is not on PATH.

    Distinct from a failed call: the app runs in a degraded but useful mode
    without Noctalia, so callers can catch this specifically.
    """


@dataclass(frozen=True, slots=True)
class ColourSchemeSelection:
    """What ``color-scheme-get`` reports: a source and a name within it."""

    source: PaletteSource
    name: str


def _executable() -> str:
    found = shutil.which("noctalia")
    if found is None:
        raise NoctaliaUnavailableError("noctalia is not on PATH")
    return found


def _run(arguments: Sequence[str], *, timeout: float) -> str:
    command = [_executable(), *arguments]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise NoctaliaError(f"noctalia {arguments[0]} timed out after {timeout}s") from error
    except OSError as error:
        raise NoctaliaError(f"cannot run noctalia: {error}") from error

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        summary = detail.splitlines()[-1] if detail else f"exit {completed.returncode}"
        raise NoctaliaError(f"noctalia {' '.join(arguments)}: {summary}")

    return completed.stdout.decode("utf-8", "replace")


def is_available() -> bool:
    return shutil.which("noctalia") is not None


def generate(image: Path, scheme: str = DEFAULT_SCHEME, *, pure_black: bool = False) -> PalettePair:
    """Generate the dark and light palettes for ``image``.

    This is the same code path Noctalia uses for its own wallpaper-derived
    palettes, so the result is exactly what the shell would pick -- not an
    approximation. It does not need Noctalia to be running.
    """
    if scheme not in ALL_SCHEMES:
        raise NoctaliaError(f"unknown scheme {scheme!r}")
    if not image.is_file():
        raise NoctaliaError(f"not a file: {image}")

    arguments = ["theme", str(image), "--scheme", scheme, "--both"]
    if pure_black:
        arguments.append("--pure-black")

    document = _run(arguments, timeout=GENERATE_TIMEOUT)
    try:
        return PalettePair.from_json(document)
    except PaletteError as error:
        raise NoctaliaError(f"could not parse generated palette: {error}") from error


def message(command: str, *arguments: str) -> str:
    """Send an IPC command to the running shell and return its stdout."""
    return _run(["msg", command, *arguments], timeout=MESSAGE_TIMEOUT).strip()


def current_wallpaper(connector: str | None = None) -> Path | None:
    """The default wallpaper path, or the effective one for a given output."""
    arguments = [connector] if connector else []
    reply = message("wallpaper-get", *arguments)
    return Path(reply) if reply else None


def set_wallpaper(path: Path, connector: str | None = None) -> None:
    """Hand a static wallpaper to Noctalia.

    Routed through Noctalia rather than set directly so its transition runs and
    -- more importantly -- so it regenerates the palette, which is what fires
    our template's post-hook and keeps the app's colours in sync.
    """
    arguments = [connector] if connector else []
    message("wallpaper-set", *arguments, str(path))


def current_scheme_selection() -> ColourSchemeSelection:
    """Parse ``color-scheme-get``, which replies ``<source> <name>``."""
    reply = message("color-scheme-get")
    source, _, name = reply.partition(" ")
    if source not in ("builtin", "wallpaper", "community", "custom"):
        raise NoctaliaError(f"unexpected colour scheme source {source!r}")
    return ColourSchemeSelection(source=source, name=name.strip())  # type: ignore[arg-type]


def current_mode() -> Mode:
    reply = message("theme-mode-get").strip()
    if reply not in ("dark", "light"):
        raise NoctaliaError(f"unexpected theme mode {reply!r}")
    return reply  # type: ignore[return-value]


def reload_config() -> None:
    message("config-reload")


def apply_templates() -> None:
    """Re-render every configured template for the current palette."""
    message("templates-apply")
