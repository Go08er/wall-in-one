"""A Workshop folder that holds no wallpaper is not a place the scan "couldn't look" at.

``Library.skipped`` is "couldn't look": a non-empty one makes the deployed-profile
upgrade refuse (it cannot prove a complete snapshot), postpones the capture
cleanup, and puts "N skipped" in the classic window. An emptied folder Steam
left after an unsubscribe, and a folder somebody made by hand, were looked at.
They stay in ``Library.unseen`` (kind NOT_A_WALLPAPER) and out of ``skipped``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from wall_in_one import config
from wall_in_one.library import scan
from wall_in_one.library.model import UnseenKind
from wall_in_one.session import Session


def _steam(home: Path) -> Path:
    content = home / ".local/share/Steam/steamapps/workshop/content/431960"
    (content / "2000000001").mkdir(parents=True)
    (content / "2000000001" / "project.json").write_text(
        json.dumps({"type": "video", "title": "v", "file": "v.mp4"})
    )
    (content / "2000000001" / "v.mp4").write_bytes(b"x")
    (content / "2000000002").mkdir()  # left behind after unsubscribing, or mid-download
    (content / "my-backup").mkdir()  # a folder a person made
    return content


def test_empty_and_hand_made_folders_are_unseen_but_not_skipped() -> None:
    """Before: both were in ``skipped``, so the upgrade refused and the cleanup waited."""
    home = Path(os.environ["HOME"])
    content = _steam(home)
    root = home / "library"
    root.mkdir()
    (root / "still.png").write_bytes(b"\x89PNG")

    library = scan.scan((root,), include_workshop=True, records={})

    assert sorted(item.path.name for item in library.items) == ["still.png", "v.mp4"]
    assert library.skipped == ()
    assert {place.path: place.kind for place in library.unseen} == {
        content / "2000000002": UnseenKind.NOT_A_WALLPAPER,
        content / "my-backup": UnseenKind.NOT_A_WALLPAPER,
    }


def test_a_session_over_them_has_nothing_skipped() -> None:
    """The check the capture cleanup and the upgrade make is on this."""
    home = Path(os.environ["HOME"])
    _steam(home)
    root = home / "library"
    root.mkdir()
    (root / "still.png").write_bytes(b"\x89PNG")
    session = Session(config.Settings(roots=(root,)))
    try:
        assert session.refresh().skipped == ()
    finally:
        session.shutdown()


def test_an_unreadable_workshop_folder_is_still_skipped() -> None:
    """Control: "couldn't look" keeps its meaning."""
    home = Path(os.environ["HOME"])
    content = _steam(home)
    root = home / "library"
    root.mkdir()
    (content / "2000000001").chmod(0)
    try:
        library = scan.scan((root,), include_workshop=True, records={})
    finally:
        (content / "2000000001").chmod(0o755)
    if os.geteuid() != 0:
        assert any("2000000001" in note for note in library.skipped)
