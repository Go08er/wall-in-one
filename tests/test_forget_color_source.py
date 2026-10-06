"""`config.forget_color_source`: Forget Display's colors step, decided under the settings lock.

It stops the desktop colors following a forgotten display only while they
still do and displays are mirrored. Anything else is a newer choice that
wins, and nothing is written; in particular, independent displays are never
left without their colors' display, which `Settings.validated` would turn
back into mirrored ones (review Y-2).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, paths

MIRRORED = config.DISPLAY_MODE_MIRRORED
INDEPENDENT = config.DISPLAY_MODE_INDEPENDENT


def _saved(mode: str, colors: str, *, extra: bytes = b"") -> bytes:
    target = config.save(
        replace(config.Settings(), display_mode=mode, theme_source_connector=colors)
    )
    if extra:
        target.write_bytes(target.read_bytes() + extra)
    return target.read_bytes()


def test_the_colors_display_is_cleared_while_it_is_still_followed_and_mirrored() -> None:
    _saved(MIRRORED, "DP-2")
    held, wrote = config.forget_color_source("DP-2")
    assert wrote and (held.display_mode, held.theme_source_connector) == (MIRRORED, "")
    reread = config.load_strict()
    assert (reread.display_mode, reread.theme_source_connector) == (MIRRORED, "")


@pytest.mark.parametrize(
    ("mode", "colors"),
    [(MIRRORED, "DP-1"), (INDEPENDENT, "DP-1"), (INDEPENDENT, "DP-2"), (MIRRORED, "")],
    ids=["another-display", "independent-elsewhere", "independent-on-it", "none"],
)
def test_anything_newer_wins_and_nothing_is_written(mode: str, colors: str) -> None:
    """Independent displays on DP-2 stay independent: a Forget never changes the mode."""
    before = _saved(mode, colors)
    held, wrote = config.forget_color_source("DP-2")
    assert not wrote
    assert (held.display_mode, held.theme_source_connector) == (mode, colors)
    assert paths.settings_path().read_bytes() == before


def test_read_only_settings_refuse_only_a_write_that_is_needed() -> None:
    unknown = b"future_setting = true\n"
    before = _saved(MIRRORED, "DP-1", extra=unknown)
    assert config.forget_color_source("DP-2")[1] is False
    assert paths.settings_path().read_bytes() == before

    paths.settings_path().unlink()  # read-only: nothing writes it, so start it afresh
    before = _saved(MIRRORED, "DP-2", extra=unknown)
    with pytest.raises(config.SettingsReadOnlyError):
        config.forget_color_source("DP-2")
    assert paths.settings_path().read_bytes() == before


def test_no_settings_file_needs_no_write(tmp_path: Path) -> None:
    assert not paths.settings_path().exists()
    held, wrote = config.forget_color_source("DP-2")
    assert not wrote and held.theme_source_connector == ""
    assert not paths.settings_path().exists()
