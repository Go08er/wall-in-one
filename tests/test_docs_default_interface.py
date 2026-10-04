"""The user docs describe the new interface as the default and say how to leave it.

0.3.0 made the new interface the default. The two sentences that matter most to
someone it doesn't work for are pinned here: the way back to Classic from
inside the app, and ``--ui=classic`` when the new interface won't open. Both
name the real Settings row (`INTERFACE_CHOICES`) and the real flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from wall_in_one import cli
from wall_in_one.ui.window_services import DEFAULT_UI

ROOT: Final = Path(__file__).resolve().parents[1]
#: Word for word in the README and in docs/updating.md.
WAY_BACK: Final = (
    "If the new interface doesn't open, start `wall-in-one --ui=classic` and choose "
    "Classic in Settings → Appearance → Interface."
)


def _text(name: str) -> str:
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def _using_it() -> str:
    readme = _text("README.md")
    return readme.split("## Using it", 1)[1].split(" ## ", 1)[0]


def test_the_way_back_to_classic_is_in_the_readme_and_the_update_guide() -> None:
    assert DEFAULT_UI == "next"
    assert WAY_BACK in _using_it()
    assert WAY_BACK in _text("docs/updating.md")
    assert "Settings → Appearance → Interface → Classic" in _using_it()


def test_the_ui_flag_the_docs_name_is_the_one_the_command_line_takes() -> None:
    parser = cli._build_parser()
    [flag] = [action for action in parser._actions if "--ui" in action.option_strings]
    assert flag.choices is not None and set(flag.choices) == {"classic", "next"}
    assert "--ui" in parser.format_help(), "the docs point at --help for it"
    listed = "overrides the saved choice for that start, and `wall-in-one --help` lists it"
    assert f"`--ui=classic` {listed}" in _text("README.md")
    assert "(listed in `wall-in-one --help`)" in _text("docs/updating.md")


def test_using_it_describes_the_new_interface_first() -> None:
    section = _using_it()
    assert section.startswith(" Wall-in-One opens on your Library. The sidebar lists the pages")
    for page in ("**Library**", "**Store**", "**Playlists**", "**Schedule**", "**Displays**"):
        assert page in section, page
    # Classic is mentioned once, as the alternative, not as the description.
    assert section.count("bottom navigation") == 1
    assert "The classic window has the same workflows on a bottom navigation" in section
