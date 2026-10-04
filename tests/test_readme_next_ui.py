"""The docs say what the new interface really does.

Final 0.2.0 review D-2: it said the playback controls were "shown but off",
while the player bar's transport controls are live. The controls named here are
the ones the bar's `PlaybackControls` offers, so a control added there needs a
word here, and the read-only restrictions are the ones the window enforces.

0.3.0 made the new interface the default. What each page does moved from the
README's development section into the user guide, ``docs/interface.md``
(`_guide`); the README's "The new interface (the default)" (`_section`) keeps
how it is built and how the interface is chosen. Each sentence is pinned where
it now lives.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Final

from wall_in_one.ui.next import state

README: Final = Path(__file__).resolve().parents[1] / "README.md"
GUIDE: Final = Path(__file__).resolve().parents[1] / "docs" / "interface.md"

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
    section = readme.split("### The new interface (the default)", 1)[1]
    section = section.split("\n### ", 1)[0].split("\n## ", 1)[0]
    return " ".join(section.split())


def _guide() -> str:
    """docs/interface.md, the new interface page by page."""
    return " ".join(GUIDE.read_text(encoding="utf-8").split())


def test_the_readme_names_every_live_playback_control() -> None:
    offered = {
        name
        for name, member in inspect.getmembers(state.PlaybackControls)
        if inspect.isfunction(member) and not name.startswith("_")
    }
    assert offered == set(CONTROLS), "name the new control in the README and here"
    section = _guide()
    assert "shown but off" not in section
    assert "Its only changes are" not in section
    for words in CONTROLS.values():
        for word in words:
            assert word in section, word


def test_the_readme_keeps_the_real_restrictions() -> None:
    section = _guide()
    assert "turns Apply, favorites, library changes and the playback controls off" in section
    assert "Settings keys this version doesn't know are named in that notice" in section
    assert "off until the wallpaper service has answered, and while it isn't running" in section
    assert "Apply, favorites and playback have no undo yet." in section
    assert "the Playlists page says so too and changes nothing" in section
    assert "Removing has no Undo" in section


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
    section = _guide()
    assert "Every playback control acts on the displays the player bar is scoped to" in section
    assert "act on all of them" not in section


def test_the_readme_says_how_to_choose_the_interface_and_what_overrides_it() -> None:
    """The interface is chosen in Settings; --ui overrides it; 0.2.0 and 0.2.1 ignore it.

    0.3.0 made the new interface the default, so the README now says how to
    choose Classic (it said how to choose "New (preview)") and that a start
    with no choice opens the new interface.
    """
    section = _section()
    assert "Settings → Appearance → Interface → Classic" in section
    assert "New (preview)" not in section and "classic stays the default" not in section
    assert "With neither, the app opens the new interface" in section
    assert "takes effect the next time Wall-in-One starts" in section
    assert "`--ui=classic` overrides the saved choice for that start" in section
    assert "When no app is running, `ctl open` launches the interface chosen in Settings" in section
    assert "0.2.0 and 0.2.1 don't know the choice and start classic" in section
    assert "It keeps a saved Classic only until it next saves a window preference" in section
    # Codex 0.3.0 round 1, D-1: the order is the flag, the saved choice, then the default.
    assert (
        "Without the flag, the app opens the interface saved in Settings, otherwise the "
        "default, which is now New"
    ) in section


def test_the_readme_says_what_the_playlists_page_does() -> None:
    """R5: Playlists is ported; the docs say so, and what it changes."""
    guide = _guide()
    assert "Playlists say" not in guide and "Store, Playlists" not in guide
    for words in (
        "with Undo",
        "its own interval and shuffle",
        '"Your pick" stays hidden',
        "can't lose its last wallpaper that plays",
        "a video waiting for its still doesn't play yet, so it can't be added",
        "A wallpaper dropped below the last row shown goes to the end of the playlist",
    ):
        assert words in guide, words
    assert (
        "Library (`media`), the Store (`browse`), Playlists (`playlists`), Settings" in _section()
    )


def test_the_readme_says_what_the_store_does() -> None:
    """The Store is ported (R5): the README describes it in the terms its code keeps."""
    from wall_in_one.ui.next import store_state

    section = _guide()
    assert "Store, Playlists" not in section, "the Store is no longer a placeholder"
    assert f"Results come {store_state.PAGE_SIZE} to a page with Previous and Next" in section
    assert "inline under the provider row (never in a popover)" in section
    assert "one at a time" in section and "no pretend progress or cancel" in section
    assert f"Wallhaven's {len(store_state.COLOURS)} search colours" in section
    assert "says to add one in Settings → Library, with a button" in section
    assert "the Store (`browse`)" in _section()


def test_the_readme_says_what_schedule_and_displays_do_and_refuse() -> None:
    section = _guide()
    # The last pages to port: nothing says it is missing from the new interface any more.
    assert "not in the new interface yet" not in section
    assert "not in the new interface yet" not in _section()
    # Schedule: the runtime's answer, and what can't be saved.
    assert 'The "Now" card and its "Why?" are the runtime\'s answer' in section
    assert "starts and ends on the same minute, or a playlist with nothing to play" in section
    # Displays: what is never stored where, and what is refused.
    assert "kept in `runtime-overrides.toml`, never in `runtime.toml`" in section
    # Codex 0.3.0 round 1, D-2: a saved display is kept even while disconnected,
    # so "refused while no display is connected" was too absolute.
    assert "refused while no display is connected" not in section
    assert (
        "the one saved, even while it is disconnected, or else the first connected one. "
        "With neither, it is refused"
    ) in section
    assert "runtime choices, never saved" in section
    assert "and so do the Schedule and Displays pages" in section
    assert "the same settings, saved the same way, as Settings → Displays" in section
