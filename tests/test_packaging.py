"""The two files a desktop shell needs before this app can be launched at all.

Neither the entry nor the icon is read by the running program, so nothing else
in the suite would notice them rotting. What holds them together is a set of
names that have to agree: the entry's filename and its Icon key are the
application id, its Exec is the console script pyproject installs, and the icon
file is named for the id as well. Those agreements are what is checked here.
"""

from __future__ import annotations

import configparser
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from xml.etree import ElementTree

import pytest

import wall_in_one
from wall_in_one import paths

DATA_DIR = Path(wall_in_one.__file__).resolve().parent / "data"
DESKTOP_PATH = DATA_DIR / f"{paths.APPLICATION_ID}.desktop"
ICON_PATH = DATA_DIR / f"{paths.APPLICATION_ID}.svg"
SYSTEMD_PATH = DATA_DIR / "systemd" / "wall-in-one.service"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"

SVG = "http://www.w3.org/2000/svg"

#: Main categories from the desktop-entry spec, of which an entry needs at
#: least one to be filed anywhere sensible.
MAIN_CATEGORIES = frozenset(
    {
        "AudioVideo",
        "Audio",
        "Video",
        "Development",
        "Education",
        "Game",
        "Graphics",
        "Network",
        "Office",
        "Science",
        "Settings",
        "System",
        "Utility",
    }
)


class DesktopParser(configparser.RawConfigParser):
    """A parser that reads a desktop entry the way the spec describes one.

    Raw because Exec lines carry ``%`` field codes that would otherwise be read
    as interpolation, and case-preserving because desktop-entry keys are case
    sensitive where ConfigParser's defaults are not.
    """

    def optionxform(self, optionstr: str) -> str:
        return optionstr


@pytest.fixture(scope="module")
def entry() -> configparser.SectionProxy:
    parser = DesktopParser()
    parser.read_string(DESKTOP_PATH.read_text(encoding="utf-8"))
    return parser["Desktop Entry"]


@pytest.fixture(scope="module")
def icon() -> ElementTree.Element:
    return ElementTree.parse(ICON_PATH).getroot()


def test_the_entry_is_named_for_the_application_id() -> None:
    # A compositor associates a window with a launcher entry by matching the
    # app-id against this filename and nothing else, so the two are one fact.
    assert DESKTOP_PATH.name == "dev.goober.WallInOne.desktop"
    assert DESKTOP_PATH.name == f"{paths.APPLICATION_ID}.desktop"
    assert DESKTOP_PATH.is_file()


def test_the_entry_parses_as_an_ini_file_with_the_keys_a_launcher_reads(
    entry: configparser.SectionProxy,
) -> None:
    assert entry["Type"] == "Application"
    assert entry["Name"] == "Wall-in-One"
    assert entry["Comment"].strip()
    assert entry["Terminal"] == "false"
    assert entry["Exec"].strip()
    assert entry["Icon"].strip()


def test_the_entry_runs_the_console_script_this_project_installs(
    entry: configparser.SectionProxy,
) -> None:
    command = entry["Exec"].split()[0]
    assert command == paths.APP_ID
    assert entry["TryExec"] == command


@pytest.mark.skipif(not PYPROJECT_PATH.is_file(), reason="not running from a source tree")
def test_the_command_the_entry_runs_is_a_script_pyproject_installs() -> None:
    metadata = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert paths.APP_ID in metadata["project"]["scripts"]


def test_the_icon_key_names_the_icon_that_ships_beside_the_entry(
    entry: configparser.SectionProxy,
) -> None:
    # An Icon without an extension is a theme lookup, which is why the file is
    # installed as <app-id>.svg under hicolor rather than referenced by path.
    assert entry["Icon"] == paths.APPLICATION_ID
    assert ICON_PATH.name == f"{entry['Icon']}.svg"
    assert ICON_PATH.is_file()


def test_the_categories_file_the_app_under_a_real_main_category(
    entry: configparser.SectionProxy,
) -> None:
    categories = entry["Categories"]
    assert categories.endswith(";"), "list values are semicolon terminated, trailing one included"
    named = set(categories.rstrip(";").split(";"))
    assert named & MAIN_CATEGORIES
    # DesktopSettings is an additional category and is only meaningful next to
    # Settings; desktop-file-validate treats the pair as required.
    if "DesktopSettings" in named:
        assert "Settings" in named


def test_the_entry_claims_no_dbus_activation_it_cannot_service(
    entry: configparser.SectionProxy,
) -> None:
    # GtkApplication exports org.gtk.Actions on the session bus either way, but
    # being activatable additionally needs a D-Bus service file naming the
    # binary. Nothing here ships one, so the key must stay away.
    assert entry.get("DBusActivatable", "false") == "false"
    assert not list(DATA_DIR.glob("*.service"))


def test_the_systemd_unit_runs_the_windowless_service() -> None:
    parser = DesktopParser()
    parser.read_string(SYSTEMD_PATH.read_text(encoding="utf-8"))
    service = parser["Service"]
    assert service["Type"] == "simple"
    assert service["ExecStart"] == "wall-in-one-service --wait-for-config"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5"
    assert parser["Unit"]["StartLimitIntervalSec"] == "0"
    assert parser["Install"]["WantedBy"] == "graphical-session.target"


def test_the_icon_is_an_svg_that_parses_and_scales(icon: ElementTree.Element) -> None:
    assert icon.tag == f"{{{SVG}}}svg"
    assert icon.get("viewBox") == "0 0 128 128"
    # Without a viewBox the icon would not be scalable at all, and a square one
    # is what every icon size a theme asks for expects.
    assert icon.get("width") == icon.get("height")
    assert len(list(icon.iter())) > 1


def test_the_icon_reaches_for_nothing_outside_itself(icon: ElementTree.Element) -> None:
    # An icon is rendered by whatever loader the shell happens to have, often
    # with no network and no fonts. Anything it cannot resolve renders as a gap.
    for element in icon.iter():
        for name, value in element.attrib.items():
            assert "href" not in name, f"{element.tag} links out through {name}"
            if value.startswith("url("):
                assert value.startswith("url(#"), f"{name} points outside the document"
    assert "<text" not in ICON_PATH.read_text(encoding="utf-8")


def test_the_icon_is_drawn_in_colour_rather_than_one_fixed_foreground(
    icon: ElementTree.Element,
) -> None:
    # A monochrome glyph would need to know the panel's colour, which it cannot;
    # several mid-tone fills read against a light panel and a dark one alike.
    painted = {
        value
        for element in icon.iter()
        for name, value in element.attrib.items()
        if name in {"fill", "stop-color", "stroke"} and value.startswith("#")
    }
    assert len(painted) >= 4
    assert "#000000" not in painted


@pytest.mark.skipif(not PYPROJECT_PATH.is_file(), reason="not running from a source tree")
def test_the_distribution_carries_both_files() -> None:
    # The Nix package installs them from the source tree, but a wheel built
    # from here has to hold them too, or the tests above pass against files the
    # installed package does not have.
    metadata = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    patterns = metadata["tool"]["setuptools"]["package-data"]["wall_in_one"]
    for path in (DESKTOP_PATH, ICON_PATH, SYSTEMD_PATH):
        relative = f"data/{path.name}"
        if path is SYSTEMD_PATH:
            relative = f"data/systemd/{path.name}"
        assert any(fnmatch(relative, pattern) for pattern in patterns), relative
