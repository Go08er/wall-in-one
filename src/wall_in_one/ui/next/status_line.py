"""One line of live runtime status for the new interface, from the model alone.

Everything here is read from the runtime's own snapshot: what it says is
playing, on how many displays, and its power policy. Nothing is resolved in
Python; the schedule and the playlist in force are the runtime's answer.

GTK-free, so the wording is tested in the ordinary suite.
"""

from __future__ import annotations

from typing import Final

from wall_in_one.ui.status_model import RuntimeStatusView

#: The runtime's playback states, as words. ``mixed`` means the displays
#: disagree (some playing, some not), so the line says so.
_STATES: Final = {
    "playing": "playing",
    "paused": "paused",
    "stopped": "stopped",
    "mixed": "partly playing",
}


def describe(view: RuntimeStatusView) -> str:
    """Say what the runtime reports, e.g. ``2 displays · playing Evening``."""
    if view.service == "unavailable":
        headline = "Wallpaper service not running"
    elif view.status is None:
        headline = (
            "Runtime status invalid" if view.protocol_error else "Checking the wallpaper service…"
        )
    else:
        headline = _playing(view, view.status)
    marks: list[str] = []
    if view.delayed:
        marks.append("status delayed")
    if view.protocol_error and view.status is not None and view.service != "unavailable":
        marks.append("invalid status reply")
    if view.busy:
        marks.append("sending a playback command…")
    return " · ".join((headline, *marks))


def _playing(view: RuntimeStatusView, status: dict[str, object]) -> str:
    truth = view.truth
    raw_playlist = status.get("playlist")
    playlist = truth.playlist if truth is not None else raw_playlist
    if not isinstance(playlist, str) or not playlist:
        playlist = "an unnamed playlist"
    reported = status.get("playback_state")
    state = (
        reported
        if isinstance(reported, str) and reported in _STATES
        else "paused"
        if status.get("paused") is True
        else "playing"
    )
    parts: list[str] = []
    if truth is not None and truth.displays:
        connected = sum(1 for display in truth.displays if display.connected)
        parts.append(f"{connected} display{'' if connected == 1 else 's'}")
    parts.append(f"{_STATES[state]} {playlist}")
    power = view.power
    if power is not None and power.message:
        parts.append(power.message)
    return " · ".join(parts)
