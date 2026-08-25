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

import contextlib
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
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
CancelCheck = Callable[[], bool]

#: Generous. Palette generation measures ~0.25s; this only bounds a hang.
GENERATE_TIMEOUT: Final = 30.0
#: IPC round-trips are sub-millisecond when the shell is up, and fail fast when
#: it is not.
MESSAGE_TIMEOUT: Final = 10.0

# Active CLI children are few (the application serialises live-theme work),
# but they still need explicit ownership.  ThreadPoolExecutor workers are
# joined at interpreter exit even after ``shutdown(wait=False)``; without this
# registry a wedged 30-second palette generation can therefore keep the whole
# graphical process alive after its last window has closed.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[int, subprocess.Popen[bytes]] = {}
_CANCEL_GENERATION = 0


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


def _signal_group(process: subprocess.Popen[bytes], requested: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        # Every child below starts its own session.  Killing the group also
        # catches a CLI wrapper which has spawned the real Noctalia process.
        os.killpg(process.pid, requested)
    except OSError, ProcessLookupError:
        with contextlib.suppress(OSError):
            process.send_signal(requested)


def cancel_pending() -> None:
    """Cancel every currently running Noctalia CLI call.

    This is a process-shutdown hook, not ordinary error recovery.  New calls
    remain possible (tests and the explicit compatibility service reuse this
    module), while a generation closes the spawn-versus-cancel race.
    """
    global _CANCEL_GENERATION
    with _ACTIVE_LOCK:
        _CANCEL_GENERATION += 1
        active = tuple(_ACTIVE.values())
    for process in active:
        _signal_group(process, signal.SIGKILL)


def _run(
    arguments: Sequence[str],
    *,
    timeout: float,
    cancelled: CancelCheck | None = None,
) -> str:
    command = [_executable(), *arguments]
    with _ACTIVE_LOCK:
        if cancelled is not None and cancelled():
            raise NoctaliaError(f"noctalia {arguments[0]} was cancelled")
        generation = _CANCEL_GENERATION
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise NoctaliaError(f"cannot run noctalia: {error}") from error

    with _ACTIVE_LOCK:
        cancelled_before_registration = generation != _CANCEL_GENERATION or (
            cancelled is not None and cancelled()
        )
        _ACTIVE[process.pid] = process
    if cancelled_before_registration:
        _signal_group(process, signal.SIGKILL)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _signal_group(process, signal.SIGTERM)
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                process.communicate()
            raise NoctaliaError(f"noctalia {arguments[0]} timed out after {timeout}s") from error
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(process.pid, None)

    with _ACTIVE_LOCK:
        was_cancelled = generation != _CANCEL_GENERATION or (cancelled is not None and cancelled())
    if was_cancelled:
        raise NoctaliaError(f"noctalia {arguments[0]} was cancelled")

    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()
        summary = detail.splitlines()[-1] if detail else f"exit {process.returncode}"
        raise NoctaliaError(f"noctalia {' '.join(arguments)}: {summary}")

    return stdout.decode("utf-8", "replace")


def is_available() -> bool:
    return shutil.which("noctalia") is not None


def generate(
    image: Path,
    scheme: str = DEFAULT_SCHEME,
    *,
    pure_black: bool = False,
    cancelled: CancelCheck | None = None,
) -> PalettePair:
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

    document = _run(arguments, timeout=GENERATE_TIMEOUT, cancelled=cancelled)
    try:
        return PalettePair.from_json(document)
    except PaletteError as error:
        raise NoctaliaError(f"could not parse generated palette: {error}") from error


def message(
    command: str,
    *arguments: str,
    cancelled: CancelCheck | None = None,
) -> str:
    """Send an IPC command to the running shell and return its stdout."""
    return _run(
        ["msg", command, *arguments],
        timeout=MESSAGE_TIMEOUT,
        cancelled=cancelled,
    ).strip()


def current_wallpaper(
    connector: str | None = None,
    *,
    cancelled: CancelCheck | None = None,
) -> Path | None:
    """The default wallpaper path, or the effective one for a given output."""
    arguments = [connector] if connector else []
    reply = message("wallpaper-get", *arguments, cancelled=cancelled)
    return Path(reply) if reply else None


def set_wallpaper(path: Path, connector: str | None = None) -> None:
    """Hand a static wallpaper to Noctalia.

    Routed through Noctalia rather than set directly so its transition runs and
    -- more importantly -- so it regenerates the palette, which is what fires
    our template's post-hook and keeps the app's colours in sync.
    """
    arguments = [connector] if connector else []
    message("wallpaper-set", *arguments, str(path))


def current_scheme_selection(*, cancelled: CancelCheck | None = None) -> ColourSchemeSelection:
    """Parse ``color-scheme-get``, which replies ``<source> <name>``."""
    reply = message("color-scheme-get", cancelled=cancelled)
    source, _, name = reply.partition(" ")
    if source not in ("builtin", "wallpaper", "community", "custom"):
        raise NoctaliaError(f"unexpected colour scheme source {source!r}")
    return ColourSchemeSelection(source=source, name=name.strip())  # type: ignore[arg-type]


def current_mode(*, cancelled: CancelCheck | None = None) -> Mode:
    reply = message("theme-mode-get", cancelled=cancelled).strip()
    if reply not in ("dark", "light"):
        raise NoctaliaError(f"unexpected theme mode {reply!r}")
    return reply  # type: ignore[return-value]


def set_scheme(selection: ColourSchemeSelection) -> None:
    """Ask Noctalia to use a palette. It regenerates and fires our template.

    The counterpart of `current_scheme_selection`, and the reason a pairing can
    carry a palette at all: everything else about a wallpaper is ours, but the
    palette is Noctalia's, and this is the only public way to move it.
    """
    message("color-scheme-set", selection.source, selection.name)


def set_mode(mode: Mode | Literal["auto"]) -> None:
    """Set dark, light, or let Noctalia decide."""
    message("theme-mode-set", mode)


def reload_config() -> None:
    message("config-reload")


def apply_templates() -> None:
    """Re-render every configured template for the current palette."""
    message("templates-apply")
