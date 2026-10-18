"""Which screens there are, so a wallpaper can be aimed at one of them.

Every renderer this app drives already accepts a connector: Noctalia takes one
on `wallpaper-set [connector]`, mpvpaper takes an output selector, and
`linux-wallpaperengine` takes `--screen-root`. What was missing was any way to
find out what the connectors *are*. Noctalia and mpvpaper accept an all-output
target, but linux-wallpaperengine's positional fallback is a preview window;
its caller therefore expands an empty configured output into these connector
names and refuses safely if none can be discovered.

There is one source of truth, the wallpaper service. It is a Wayland client of
the compositor and reads the standard output protocols (`wl_output`,
xdg-output, and wlr-output-management where the compositor offers it), so this
works on any compositor Noctalia supports and asks none of them anything of
its own. It publishes what it found in its status as ``outputs``;
:func:`discover` asks it for that. The window adds GTK's own monitors
(`wall_in_one.ui.monitors`) as the fallback for when no service answers; a
process without a window gets nothing then, and each renderer chooses its own
safe degradation, exactly as when the compositor could not be asked before.

**Multi-screen behaviour is verified against the protocols, not against
monitors.** The service's discovery is tested over the wire against a fake
compositor with several outputs, switched-off heads and hot-plug, and against
a real one with a single output; nobody has yet plugged a second monitor into
a machine running this.

See ``docs/display-discovery.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from wall_in_one.control import client

#: How long to wait for the service's status. It answers from memory; this
#: bounds a service that is busy or wedged, not the question.
QUERY_TIMEOUT: Final = 5.0

#: The service's own ceiling on enabled outputs; a status carries no more
#: rows than this, and the ceiling stops a malformed one becoming an
#: unbounded loop.
MAX_OUTPUTS: Final = 64
MAX_REFRESH_MILLIHZ: Final = 1_000_000
#: The longest connector name any renderer is handed (the service's bound).
MAX_NAME_BYTES: Final = 256


@dataclass(frozen=True, slots=True)
class Output:
    """One screen, named the way every renderer here wants it named."""

    #: The connector: `eDP-1`, `DP-2`. What gets passed to the renderers.
    name: str
    make: str = ""
    model: str = ""
    #: Logical size, which is the size after scaling and therefore the one that
    #: describes what somebody sees.
    width: int = 0
    height: int = 0
    scale: float = 1.0
    #: Pixel dimensions of the active mode. Scene still capture needs these:
    #: using linux-wallpaperengine's default window produced a portrait image
    #: that was then cropped and enlarged onto a landscape display.
    physical_width: int = 0
    physical_height: int = 0
    #: Refresh rate of the active mode in millihertz, as the compositor
    #: reports it. Kept integral so a value such as 165.004 Hz survives
    #: without rounding.
    refresh_millihz: int = 0
    #: False for a head the compositor reports as switched off: it is listed,
    #: never rendered on.
    enabled: bool = True

    @property
    def label(self) -> str:
        """What to call this screen in a list somebody reads.

        The connector always appears, because that is the name in the config
        file and in every renderer's arguments, and a settings dialogue that
        says "BOE 0x0A9B" while the config says `eDP-1` is a puzzle.
        """
        described = " ".join(part for part in (self.make, self.model) if part)
        size = f"{self.width}x{self.height}" if self.width and self.height else ""
        state = "" if self.enabled else "off"
        detail = ", ".join(part for part in (described, size, state) if part)
        return f"{self.name} ({detail})" if detail else self.name


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _as_position(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value if character.isprintable()).strip()


def _as_refresh(value: object) -> int:
    refresh = _as_int(value)
    return refresh if 0 < refresh <= MAX_REFRESH_MILLIHZ else 0


def _as_scale(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    scale = float(value)
    return scale if 0.0 < scale < 64.0 else 1.0


def _as_connector(value: object) -> str:
    """A connector name a renderer can be given, or ``""``.

    The service's own rule: bounded, and nothing a command line or a
    tab-separated listing could misread.
    """
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if (
        not name
        or len(name.encode()) > MAX_NAME_BYTES
        or any(character.isspace() or not character.isprintable() for character in name)
    ):
        return ""
    return name


def _ordered(found: Iterable[Output]) -> tuple[Output, ...]:
    """One row per connector, sorted by name.

    A display list that reshuffles between openings is worse than one in an
    arbitrary but fixed order, and nothing promises the same order twice.
    """
    unique: dict[str, Output] = {}
    for output in found:
        if len(unique) >= MAX_OUTPUTS:
            break
        unique.setdefault(output.name, output)
    return tuple(sorted(unique.values(), key=lambda output: output.name))


def from_status(status: object) -> tuple[Output, ...] | None:
    """The service's display snapshot, from its status reply.

    ``None`` when the status carries none at all: not a status, or one from a
    service older than the snapshot. An empty tuple is a service that has not
    discovered anything yet. Rows that are not one are left out.
    """
    if not isinstance(status, Mapping):
        return None
    rows = status.get("outputs")
    if not isinstance(rows, list):
        return None
    found: list[Output] = []
    for row in rows[: MAX_OUTPUTS * 2]:
        if not isinstance(row, Mapping):
            continue
        name = _as_connector(row.get("name"))
        if not name:
            continue
        enabled = row.get("enabled")
        found.append(
            Output(
                name=name,
                make=_as_text(row.get("make")),
                model=_as_text(row.get("model")),
                width=_as_int(row.get("width")),
                height=_as_int(row.get("height")),
                scale=_as_scale(row.get("scale")),
                physical_width=_as_int(row.get("mode_width")),
                physical_height=_as_int(row.get("mode_height")),
                refresh_millihz=_as_refresh(row.get("refresh_millihz")),
                enabled=enabled if isinstance(enabled, bool) else True,
            )
        )
    return _ordered(found)


def _call(monitor: object, method: str) -> object:
    function = getattr(monitor, method, None)
    if not callable(function):
        return None
    try:
        return function()
    except Exception:  # a binding without the method, or one that refuses
        return None


def from_monitors(monitors: Iterable[object]) -> tuple[Output, ...]:
    """GTK's monitors (`Gdk.Monitor`), as outputs.

    Only the window has these, and only on its main thread: GTK may not be
    touched anywhere else (see `wall_in_one.ui.monitors`). GTK knows the
    monitors that are on, nothing about switched-off ones. Its geometry is
    logical; the pixel size is that times the scale, which GTK 4.14 and later
    report fractionally (`get_scale`) and older versions as a whole number.
    """
    found: list[Output] = []
    for monitor in monitors:
        name = _as_connector(_call(monitor, "get_connector"))
        if not name:
            continue
        geometry = _call(monitor, "get_geometry")
        width = _as_int(getattr(geometry, "width", 0))
        height = _as_int(getattr(geometry, "height", 0))
        fractional = _call(monitor, "get_scale")
        scale = _as_scale(
            fractional if fractional is not None else _call(monitor, "get_scale_factor")
        )
        found.append(
            Output(
                name=name,
                make=_as_text(_call(monitor, "get_manufacturer")),
                model=_as_text(_call(monitor, "get_model")),
                width=width,
                height=height,
                scale=scale,
                physical_width=round(width * scale),
                physical_height=round(height * scale),
                refresh_millihz=_as_refresh(_call(monitor, "get_refresh_rate")),
            )
        )
    return _ordered(found)


def ask_service(timeout: float = QUERY_TIMEOUT) -> tuple[Output, ...] | None:
    """The running service's snapshot; ``None`` when no service answers with one.

    A plain status request on the service's own socket: it starts nothing,
    and a service that is not running is an immediate answer.
    """
    try:
        response = client.send_runtime("status", timeout=timeout)
    except client.ControlError:
        return None
    if not response.ok:
        return None
    try:
        status = json.loads(response.message)
    except ValueError, RecursionError:
        return None
    return from_status(status)


def discover(fallback: Iterable[Output] = ()) -> tuple[Output, ...]:
    """Every screen this process can learn about. Empty when nothing can be asked.

    The service's snapshot when it has one; otherwise ``fallback``, which the
    window fills from GTK's monitors on its main thread before handing the
    job off. Safe from any thread.

    Empty is a meaningful answer rather than an exception. Each renderer then
    chooses its safe degradation: Noctalia/mpvpaper can use an all-output
    target, while linux-wallpaperengine must refuse rather than open a preview
    window that only looks like an application.
    """
    found = ask_service()
    if found:
        return found
    return _ordered(fallback)


def names(found: Iterable[Output]) -> tuple[str, ...]:
    return tuple(output.name for output in found)


def unambiguous_refresh_hz(found: Iterable[Output], connector: str = "") -> float | None:
    """The one refresh rate suitable for mpv's display override.

    A named connector has one answer.  ``All outputs`` only has one when every
    connected output reports the same active rate; feeding one monitor's rate
    to an mpvpaper process spanning mixed-refresh outputs would fix one cadence
    by making the other wrong.  Unknown or mixed rates therefore leave mpv to
    detect what it can rather than inventing a number.  A switched-off head
    plays nothing and has no say.
    """
    candidates = tuple(
        output for output in found if output.enabled and (not connector or output.name == connector)
    )
    reported = tuple(output.refresh_millihz for output in candidates)
    rates = set(reported)
    if not candidates or any(rate <= 0 for rate in reported) or len(rates) != 1:
        return None
    return rates.pop() / 1000.0
