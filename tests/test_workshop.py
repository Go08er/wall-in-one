"""Reading Steam's Workshop tree, and linking back out to it.

Everything here runs against a fake Steam directory under `tmp_path`. The real
one is never read and never written -- Steam's directories belong to Steam,
and a test that touched them would be the third time this project damaged
somebody's Workshop content.
"""

from __future__ import annotations

import json
from pathlib import Path

from wall_in_one.library import workshop

# -- linking out ----------------------------------------------------------
#
# Step 16 is a link rather than a scraper: subscribing has no unauthenticated
# API, so Steam is the only place the button works.


def test_the_browse_links_name_wallpaper_engine() -> None:
    assert workshop.APP_ID in workshop.BROWSE_URL
    assert workshop.APP_ID in workshop.BROWSE_URI
    assert workshop.BROWSE_URL.startswith("https://")
    assert workshop.BROWSE_URI.startswith("steam://")


def test_an_item_link_carries_its_id() -> None:
    assert workshop.item_page_url("3021911243").endswith("id=3021911243")
    assert workshop.item_page_uri("3021911243").endswith("/3021911243")


def test_only_digits_are_trusted_in_a_link() -> None:
    """These are interpolated into a URL handed to a browser or to Steam.

    The id is read off a directory name on disk, which is not a thing to put
    in a URL unchecked.
    """
    for hostile in ("", "../../etc", "1234 5678", "abc", "12\n34", "1;2", "①②③"):
        assert not workshop.is_workshop_id(hostile)
        assert workshop.item_page_url(hostile) == ""
        assert workshop.item_page_uri(hostile) == ""


def test_a_plain_id_is_trusted() -> None:
    assert workshop.is_workshop_id("3021911243")
    assert workshop.is_workshop_id("1")


def test_steam_is_preferred_when_it_is_installed(tmp_path: Path) -> None:
    """The client overlay is the only place Subscribe does anything."""
    (tmp_path / "steamapps").mkdir(parents=True)

    preferred, fallback = workshop.links(extra_roots=(tmp_path,), include_defaults=False)

    assert preferred.startswith("steam://")
    assert fallback.startswith("https://")


def test_without_steam_both_links_are_the_web_page(tmp_path: Path) -> None:
    """A `steam://` link on a machine with no Steam opens nothing."""
    preferred, fallback = workshop.links(
        extra_roots=(tmp_path / "nowhere",), include_defaults=False
    )

    assert preferred == fallback
    assert fallback.startswith("https://")


def test_a_rejected_id_produces_no_links() -> None:
    assert workshop.links("not-an-id") == ("", "")


# -- reading the tree -----------------------------------------------------


def _install(content: Path, workshop_id: str, project: dict[str, object], file: str = "") -> Path:
    directory = content / workshop_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "project.json").write_text(json.dumps(project), encoding="utf-8")
    if file:
        (directory / file).write_bytes(b"\x00" * 16)
    return directory


def _content(tmp_path: Path) -> Path:
    return tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID


def test_a_video_item_is_read(tmp_path: Path) -> None:
    _install(
        _content(tmp_path),
        "3021911243",
        {"type": "video", "title": "Gliding Frog", "file": "frog.mp4"},
        file="frog.mp4",
    )

    found = workshop.scan((tmp_path,), include_defaults=False)

    assert len(found) == 1
    assert found[0].title == "Gliding Frog"
    assert found[0].is_video


def test_the_type_is_compared_case_folded(tmp_path: Path) -> None:
    """The real machine spells it both `video` and `Video`.

    Trusting the casing hid eight wallpapers.
    """
    _install(
        _content(tmp_path),
        "1111111111",
        {"type": "Video", "title": "Shouty", "file": "a.mp4"},
        file="a.mp4",
    )
    assert workshop.scan((tmp_path,), include_defaults=False)[0].is_video


def test_a_scene_is_reported_but_not_a_video(tmp_path: Path) -> None:
    """A scene's `file` names a scene.json that is packed inside scene.pkg."""
    directory = _install(
        _content(tmp_path),
        "1647046763",
        {"type": "scene", "title": "Toothless", "file": "scene.json"},
    )
    (directory / "scene.pkg").write_bytes(b"\x00" * 16)

    found = workshop.scan((tmp_path,), include_defaults=False)

    assert len(found) == 1
    assert not found[0].is_video
    assert workshop.videos(found) == ()


def test_an_item_with_no_project_is_skipped(tmp_path: Path) -> None:
    """Somebody mid-edit, or a directory Steam has half-removed."""
    (_content(tmp_path) / "9999999999").mkdir(parents=True)
    assert workshop.scan((tmp_path,), include_defaults=False) == ()


def test_unreadable_json_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    directory = _content(tmp_path) / "8888888888"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text("{not json", encoding="utf-8")

    assert workshop.scan((tmp_path,), include_defaults=False) == ()


def test_no_steam_at_all_is_not_an_error(tmp_path: Path) -> None:
    assert workshop.scan((tmp_path / "nowhere",), include_defaults=False) == ()


def test_nothing_is_written_into_steams_tree(tmp_path: Path) -> None:
    """Steam's directories belong to Steam."""
    content = _content(tmp_path)
    _install(content, "3021911243", {"type": "video", "title": "A", "file": "a.mp4"}, file="a.mp4")
    before = sorted(path.name for path in (content / "3021911243").iterdir())

    workshop.scan((tmp_path,), include_defaults=False)

    assert sorted(path.name for path in (content / "3021911243").iterdir()) == before
