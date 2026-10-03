"""The README's `--ui=next` section says what the preview really changes.

Final 0.2.0 review D-2: it said the playback controls were "shown but off",
while the player bar's transport controls are live. The controls named here are
the ones the bar's `PlaybackControls` offers, so a control added there needs a
word here, and the read-only restrictions are the ones the window enforces.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Final

from wall_in_one.ui.next import state

README: Final = Path(__file__).resolve().parents[1] / "README.md"

#: Each `PlaybackControls` method, and how the README names what it does.
CONTROLS: Final = {
    "toggle_play": ("play/pause",),
    "step": ("next", "previous"),
    "random": ("random",),
    "stop": ("Stop",),
    "resume_schedule": ("Resume schedule",),
    "set_shuffle": ("shuffle",),
    "set_rotate": ('"Change wallpaper automatically"',),
    # Not a transport verb: the bar's offer to start a stopped service, which
    # only says how to.
    "start_service": ("the app doesn't start the service",),
}


def _section() -> str:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("### The new interface behind `--ui=next`", 1)[1]
    section = section.split("\n### ", 1)[0].split("\n## ", 1)[0]
    return " ".join(section.split())


def test_the_readme_names_every_live_playback_control() -> None:
    offered = {
        name
        for name, member in inspect.getmembers(state.PlaybackControls)
        if inspect.isfunction(member) and not name.startswith("_")
    }
    assert offered == set(CONTROLS), "name the new control in the README and here"
    section = _section()
    assert "shown but off" not in section
    assert "Its only changes are" not in section
    for words in CONTROLS.values():
        for word in words:
            assert word in section, word


def test_the_readme_keeps_the_real_restrictions() -> None:
    section = _section()
    assert "turns Apply, favorites and the playback controls off" in section
    assert "Settings keys this version doesn't know are named in that notice" in section
    assert "off until the wallpaper service has answered, and while it isn't running" in section
    assert "There is no undo yet." in section


def test_the_readme_says_every_control_follows_the_bar_scope() -> None:
    """0.2.1 review G-2: it said Stop and automatic changing always act on all
    displays. A control without a ``scope`` parameter follows the bar's scope
    too, by the protocol's own rule, so the README states one rule for all."""
    contract = " ".join((state.PlaybackControls.__doc__ or "").split())
    assert "None (or no ``scope`` parameter at all) means `AppState.scope`" in contract
    unscoped = {
        name
        for name in CONTROLS
        if "scope" not in inspect.signature(getattr(state.PlaybackControls, name)).parameters
    }
    assert {"stop", "set_rotate"} <= unscoped, "the controls the old wording singled out"
    section = _section()
    assert "Every playback control acts on the displays the player bar is scoped to" in section
    assert "act on all of them" not in section
