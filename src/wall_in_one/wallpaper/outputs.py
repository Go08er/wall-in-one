"""Which screens there are, so a wallpaper can be aimed at one of them.

Every renderer this app drives already accepts a connector: Noctalia takes one
on `wallpaper-set [connector]`, mpvpaper takes an output selector, and
`linux-wallpaperengine` takes `--screen-root`. What was missing was any way to
find out what the connectors *are*, so all three were only ever handed the
empty string, which every one of them reads as "everywhere".

niri is the source, because niri is what this app targets and it publishes the
answer as JSON on a command anybody can run. Asking the compositor rather than
Noctalia is deliberate: Noctalia knows about outputs too, but it learns them
from the compositor, and going to the original avoids a second opinion that can
be one event behind.

**Nothing here has been exercised on more than one screen.** The development
machine has a single eDP-1, so the parsing below is verified against real niri
output and the multi-screen behaviour is verified only against recorded JSON
from a two-output layout. That is a real limitation and is written down rather
than glossed: the shape is right, the ordering rule is deliberate, and whether
it *feels* right with two monitors is untested.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

#: The compositor is being asked a question it answers from memory. A timeout
#: this short is a guard against a wedged socket, not a budget.
QUERY_TIMEOUT: Final = 5.0

#: A machine with more screens than this is not one this app was written for,
#: and the ceiling stops a malformed reply becoming an unbounded loop.
MAX_OUTPUTS: Final = 32


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

    @property
    def label(self) -> str:
        """What to call this screen in a list somebody reads.

        The connector always appears, because that is the name in the config
        file and in every renderer's arguments, and a settings dialogue that
        says "BOE 0x0A9B" while the config says `eDP-1` is a puzzle.
        """
        described = " ".join(part for part in (self.make, self.model) if part)
        size = f"{self.width}x{self.height}" if self.width and self.height else ""
        detail = ", ".join(part for part in (described, size) if part)
        return f"{self.name} ({detail})" if detail else self.name


def is_available() -> bool:
    return shutil.which("niri") is not None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse(document: object) -> tuple[Output, ...]:
    """Outputs from niri's reply, ignoring anything that is not one.

    Separate from the subprocess call so the awkward parts -- a missing
    `logical` block, a connector that is not a string, a reply that is a list
    when it should be an object -- can be tested without a compositor.

    Sorted by connector name. niri returns an object, and while CPython
    preserves insertion order there is nothing in the protocol promising the
    same order twice; a display list that reshuffles between openings is worse
    than one in an arbitrary but fixed order.
    """
    if not isinstance(document, dict):
        return ()
    found: list[Output] = []
    for key, value in document.items():
        if len(found) >= MAX_OUTPUTS:
            break
        if not isinstance(value, dict):
            continue
        # The key is the connector; `name` inside repeats it. Preferring the
        # key means an entry with a broken `name` field is still usable.
        name = _as_text(value.get("name")) or _as_text(key)
        if not name:
            continue
        logical = value.get("logical")
        block: Mapping[str, object] = logical if isinstance(logical, dict) else {}
        scale = block.get("scale")
        modes = value.get("modes")
        current = value.get("current_mode")
        mode: Mapping[str, object] = {}
        if (
            isinstance(modes, list)
            and isinstance(current, int)
            and not isinstance(current, bool)
            and 0 <= current < len(modes)
            and isinstance(modes[current], dict)
        ):
            mode = modes[current]
        found.append(
            Output(
                name=name,
                make=_as_text(value.get("make")),
                model=_as_text(value.get("model")),
                width=_as_int(block.get("width")),
                height=_as_int(block.get("height")),
                scale=float(scale) if isinstance(scale, (int, float)) and scale > 0 else 1.0,
                physical_width=_as_int(mode.get("width")),
                physical_height=_as_int(mode.get("height")),
            )
        )
    return tuple(sorted(found, key=lambda output: output.name))


def discover() -> tuple[Output, ...]:
    """Every screen niri knows about. Empty when it cannot be asked.

    Empty is a meaningful answer rather than a failure: it is what the whole
    app did before this module existed, and it means "one unnamed screen, aim
    at everything", which is exactly right on a compositor that is not niri.
    """
    if not is_available():
        return ()
    try:
        completed = subprocess.run(
            ["niri", "msg", "--json", "outputs"],
            capture_output=True,
            timeout=QUERY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if completed.returncode != 0:
        return ()
    try:
        document = json.loads(completed.stdout)
    except (ValueError, UnicodeDecodeError):
        return ()
    return parse(document)


def names(found: Iterable[Output]) -> tuple[str, ...]:
    return tuple(output.name for output in found)
