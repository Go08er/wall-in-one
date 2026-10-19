"""What the guides tell someone whose renderers are no longer in the package.

From 0.4 the renderers are installed beside the app (docs/packaging.md). An
update from 0.3 without them, or a renderer removed or moved to another
profile, leaves runtime.toml naming a renderer that is not there: every video
and scene is skipped and the skip saved. The update and install guides must
say so before it happens, and how to recover after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
#: The recovery, in both guides: compile again, then restart the service.
RECOMPILE: Final = "wall-in-one --write-config"
RESTART: Final = "systemctl --user restart wall-in-one.service"


def _text(name: str) -> str:
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def _section(text: str, heading: str) -> str:
    assert heading in text, heading
    return text.split(heading, 1)[1].split(" ## ", 1)[0]


def test_the_update_guide_says_to_install_the_renderers_before_restarting() -> None:
    updating = _text("docs/updating.md")
    section = _section(updating, "## From 0.3 to 0.4: install the renderers first")
    assert updating.index("## From 0.3 to 0.4") < updating.index("## Update and restart")
    assert "before you restart the service" in section
    assert "programs.wall-in-one.enable = true" in section
    assert "homeManagerModules.default" in section
    assert "nix profile install nixpkgs#mpvpaper nixpkgs#linux-wallpaperengine" in section
    assert "manualWallpaperInstalls = true" in section
    # What already-skipped wallpapers need.
    assert RECOMPILE in section and RESTART in section
    assert "**Try again**" in section


def test_the_install_guide_says_what_a_missing_or_moved_renderer_does() -> None:
    installing = _text("docs/installing.md")
    section = _section(installing, "#### After installing, removing or moving a renderer")
    assert "switching `manualWallpaperInstalls`" in section
    assert RECOMPILE in section and RESTART in section
    assert "**Try again**" in section
    # 96c437a said a missing renderer only "can't play its kind of wallpaper".
    assert "the service says so when one is needed" not in installing


def test_the_home_manager_units_need_no_distribution_setup() -> None:
    """The module registers them (systemd.user.packages); no genericLinux or hand links."""
    for name in ("docs/installing.md", "docs/packaging.md"):
        text = _text(name)
        assert "targets.genericLinux" not in text, name
    assert "systemd.user.packages" in _text("docs/installing.md")


def test_the_design_note_describes_the_companion_contract_not_its_behaviour() -> None:
    packaging = _text("docs/packaging.md")
    assert "so this is the value it reads" not in packaging
    assert "the value the companion is meant to prefer" in packaging
