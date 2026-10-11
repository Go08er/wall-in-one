"""A library folder that is a symbolic link: resolved once when picked, and said when saved.

A wallpaper folder spelled as a link (``~/Wallpapers -> /mnt/data/Wallpapers``)
is not scanned at all: the scan lists what is in the folder it is given, and
never follows a link at the time it acts. Adding one used to be accepted
without a word, and the library was silently empty. The link is now resolved
when the person picks it, the real folder is what is saved, and the
confirmation says so. A link already in ``settings.toml`` is left alone, and
its row says why it shows nothing and what to add instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_astra030_ac import _Chose  # noqa: E402
from tests.test_ui_codex030_round1b import _descendants  # noqa: E402
from tests.test_ui_codex030_round1c import Shown, shown, toolkit  # noqa: E402, F401
from wall_in_one import config  # noqa: E402
from wall_in_one.library import scan  # noqa: E402
from wall_in_one.ui import preferences  # noqa: E402


def _linked(tmp_path: Path) -> tuple[Path, Path]:
    """A real folder holding one picture, and a link to it."""
    real, link = tmp_path / "data" / "Wallpapers", tmp_path / "Wallpapers"
    real.mkdir(parents=True)
    (real / "sea.png").write_bytes(b"placeholder image bytes")
    link.symlink_to(real, target_is_directory=True)
    return real, link


@pytest.mark.parametrize("window", ["new", "classic", "first run"])
def test_picking_a_linked_folder_adds_the_folder_it_points_to(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
    window: str,
) -> None:
    opened = shown.opened
    application = opened.application
    real, link = _linked(tmp_path)

    if window == "new":
        access = opened.page.access
        assert access is not None
        access.add_folder(str(link))
    elif window == "classic":
        classic = preferences.PreferencesPage(application)
        classic.apply_settings(application.settings)
        classic._on_root_chosen(_Chose(link), None)  # type: ignore[arg-type]
    else:
        application._save_initial_library_root(link)
    opened.wait("the folder to be saved")

    # Failed on 97d97ef: the link was saved, and the library showed nothing from it.
    assert config.load_strict().roots == (shown.walls, shown.walls2, real)
    assert f"Added {real} (the folder {link} points to)" in opened.reports
    spin_until(lambda: application._library_scan_future is None, 10, what="the rescan")

    # Resolved once: the link is not what was saved, so swapping it changes nothing.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link.unlink()
    link.symlink_to(elsewhere, target_is_directory=True)
    found = scan.scan(config.load_strict().roots, {})
    assert found.find(real / "sea.png") is not None


def test_a_linked_folder_already_saved_says_why_it_is_empty_and_what_to_add(
    shown: Shown,  # noqa: F811
    tmp_path: Path,
) -> None:
    """From an older version, or a hand edit. The settings are not rewritten."""
    opened = shown.opened
    application = opened.application
    real, link = _linked(tmp_path)
    application.update_settings(roots=(shown.walls, shown.walls2, link))
    saved = config.load_strict().roots
    assert saved == (shown.walls, shown.walls2, link)
    told = f"a symbolic link, which is not scanned; add {real} instead"

    assert scan.scan((link,), {}).skipped == (f"{link}: {told}",)

    access = opened.page.access
    assert access is not None
    spin_until(lambda: bool(access.folder_list()[-1].note), 10, what="the folder check")
    row = access.folder_list()[-1]
    # Failed on 97d97ef: no note at all; the scan said only "not a directory".
    assert (row.path, row.note, row.missing) == (str(link), told, False)

    classic = preferences.PreferencesPage(application)
    classic.apply_settings(application.settings)
    subtitles = [
        widget.get_subtitle()
        for widget in _descendants(classic)
        if isinstance(widget, Adw.ActionRow)
    ]
    assert f"{link} -- {told}" in subtitles
    assert config.load_strict().roots == saved, "the settings are the person's"
    spin_until(lambda: application._library_scan_future is None, 10, what="the rescan")
