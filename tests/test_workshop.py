"""Reading Steam's Workshop tree, and linking back out to it.

Everything here runs against a fake Steam directory under `tmp_path`. The real
one is never read and never written -- Steam's directories belong to Steam,
and a test that touched them would be the third time this project damaged
somebody's Workshop content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wall_in_one.library import workshop
from wall_in_one.library.model import Kind, MediaItem, Unplayable

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


def test_default_roots_follow_the_current_home_instead_of_import_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "isolated-home"
    steam = home / ".local" / "share" / "Steam"
    steam.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    assert workshop.steam_roots() == (steam,)


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


# -- what the scan could not look into ------------------------------------


def test_a_listing_that_fails_is_said_and_an_absent_folder_is_not(tmp_path: Path) -> None:
    import os

    from wall_in_one.library.model import UnseenKind

    if os.geteuid() == 0:
        pytest.skip("root reads a folder with no permission bits")
    content = _content(tmp_path)
    video = _install(content, "1", {"type": "video", "file": "v.mp4"}, file="v.mp4")
    said: list[tuple[Path, UnseenKind]] = []

    def on_unseen(path: Path, kind: UnseenKind, _why: str) -> None:
        said.append((path, kind))

    assert workshop.scan((tmp_path / "nowhere",), include_defaults=False, on_unseen=on_unseen) == ()
    assert said == [], "an absent Steam is quiet"

    video.chmod(0o000)
    try:
        workshop.scan((tmp_path,), include_defaults=False, on_unseen=on_unseen)
    finally:
        video.chmod(0o700)
    assert said == [(video, UnseenKind.UNREADABLE)]

    said.clear()
    content.chmod(0o000)
    try:
        workshop.scan((tmp_path,), include_defaults=False, on_unseen=on_unseen)
    finally:
        content.chmod(0o700)
    assert said == [(content, UnseenKind.UNREADABLE)]


def test_the_item_cap_is_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wall_in_one.library.model import UnseenKind

    content = _content(tmp_path)
    for name in ("1", "2"):
        _install(content, name, {"type": "video", "file": "v.mp4"}, file="v.mp4")
    monkeypatch.setattr(workshop, "MAX_ITEMS", 1)
    said: list[tuple[Path, UnseenKind]] = []

    found = workshop.scan(
        (tmp_path,), include_defaults=False, on_unseen=lambda p, k, _w: said.append((p, k))
    )

    assert len(found) == 1 and said == [(content, UnseenKind.LIMIT)]


# -- every item is listed: web, presets, and what the engine can't run -------
#
# The owner added a `web` wallpaper and a preset of it, and neither showed up:
# the scan kept only playable videos and `scene`s (docs/wallpaper-engine.md).
# These fixtures copy the shapes of those two real items, built under
# `tmp_path`; the real Steam folder is never read.


def _web(content: Path, workshop_id: str = "2777242023") -> Path:
    return _base(content, "web", workshop_id)


def _base(content: Path, kind: str = "scene", workshop_id: str = "2777242023") -> Path:
    """A wallpaper with typed properties for a preset to set: a scene, or a web page."""
    directory = _install(
        content,
        workshop_id,
        {
            "type": kind,
            "title": "Electric Eye",
            "file": "index.html" if kind == "web" else "scene.json",
            "preview": "preview.jpg",
            "general": {
                "properties": {
                    "arcs": {"type": "bool", "value": True, "text": "Arcs"},
                    "arcsDivider": {"type": "text", "text": "<hr>"},
                    "arcsGeneralRadius": {"type": "slider", "value": 225, "min": 0, "max": 500},
                    "beamHaloRing1RotationSpeed": {"type": "slider", "value": 0.5},
                    "arcsVisualsColor1": {"type": "color", "value": "0.6 0.6 0.6"},
                    "arcsVisualsColor2": {"type": "color", "value": "0.5 0.75 1"},
                    "arcsGeneralBounceType": {
                        "type": "combo",
                        "value": 2,
                        "options": [{"label": "A", "value": 0}, {"label": "B", "value": 2}],
                    },
                    "theme": {
                        "type": "combo",
                        "value": "dark",
                        "options": [{"label": "Dark", "value": "dark"}],
                    },
                    "customImageVisualsFile": {"type": "file", "value": ""},
                }
            },
        },
        file="index.html" if kind == "web" else "",
    )
    (directory / "preview.jpg").write_bytes(b"\xff\xd8\xff")
    return directory


def _preset(content: Path, values: dict[str, object], dependency: object = "2777242023") -> Path:
    directory = _install(
        content,
        "2946165131",
        {
            "title": "Y2K Matrix",
            "preview": "preview.gif",
            "dependency": dependency,
            "preset": values,
        },
    )
    (directory / "preview.gif").write_bytes(b"GIF89a")
    return directory


def _listed(tmp_path: Path) -> dict[str, MediaItem]:
    from wall_in_one.library import scan

    return {item.scene or item.path.name: item for item in scan.workshop_items((tmp_path,))}


def test_a_web_wallpaper_is_listed_as_a_scene_the_engine_plays(tmp_path: Path) -> None:
    from wall_in_one.library import scan

    web = _web(_content(tmp_path))

    (item,) = scan.workshop_items((tmp_path,))

    assert item.kind is Kind.SCENE
    assert item.path == web and item.scene == "2777242023"
    assert item.title == "Electric Eye" and item.workshop_type == "web"
    assert item.preview == web / "preview.jpg"
    assert item.unplayable is None and item.rendered_scene == "2777242023"
    assert item.scene_properties == () and item.still_from_preview
    assert item.provider == scan.WORKSHOP_PROVIDER


def test_a_preset_is_its_own_item_playing_its_base_with_its_values(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _base(content)
    preset = _preset(
        content,
        {
            "arcs": False,
            "arcsDivider": None,
            "arcsGeneralRadius": 350,
            "beamHaloRing1RotationSpeed": -2.5,
            # Wallpaper Engine's 0-1 floats, which linux-wallpaperengine would
            # read as 0-255 integers without a decimal point.
            "arcsVisualsColor1": "0 0.5019607843137255 0",
            "arcsVisualsColor2": "1,1,0",
            "arcsGeneralBounceType": 0,
            "theme": "dark",
            # Not declared by the base, or not a type that is passed on.
            "_d0": None,
            "wec_hue": 3,
            "customImageVisualsFile": "/etc/passwd",
        },
    )

    found = _listed(tmp_path)

    item = found["2946165131"]
    assert item.kind is Kind.SCENE and item.path == preset
    assert item.title == "Y2K Matrix" and item.workshop_type == "preset"
    assert item.preview == preset / "preview.gif"
    assert item.scene == "2946165131", "its own identity, not its base's"
    assert item.scene_base == "2777242023" and item.rendered_scene == "2777242023"
    assert item.unplayable is None
    assert item.scene_properties == (
        ("arcs", "false"),
        ("arcsGeneralBounceType", "0"),
        ("arcsGeneralRadius", "350"),
        ("arcsVisualsColor1", "0.000000 0.501961 0.000000"),
        ("arcsVisualsColor2", "1.000000 1.000000 0.000000"),
        ("beamHaloRing1RotationSpeed", "-2.5"),
        ("theme", "dark"),
    )
    # The base is listed too, as itself.
    assert found["2777242023"].scene_properties == ()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("arcs", 1),  # a bool property takes a JSON boolean only
        ("arcsGeneralRadius", True),
        ("arcsGeneralRadius", "225"),
        ("arcsGeneralRadius", 1e20),  # `std::stof` would throw
        ("arcsGeneralRadius", float("nan")),
        ("arcsVisualsColor1", "1 1"),
        ("arcsVisualsColor1", "1.5 0 0"),
        ("arcsVisualsColor1", "red"),
        ("arcsVisualsColor1", "#ff0000"),
        ("arcsGeneralBounceType", 1),  # not one of the options
        ("arcsGeneralBounceType", 2.5),
        ("theme", "light"),
        ("arc=s", True),
        ("-arcs", True),
    ),
)
def test_a_value_the_engine_would_refuse_is_left_out(
    tmp_path: Path, name: str, value: object
) -> None:
    content = _content(tmp_path)
    _base(content)
    _preset(content, {name: value})

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is None
    assert item.scene_properties == ()


def test_a_preset_whose_base_is_not_installed_is_listed_needing_it(tmp_path: Path) -> None:
    _preset(_content(tmp_path), {"arcs": True})

    item = _listed(tmp_path)["2946165131"]

    assert item.kind is Kind.SCENE and item.title == "Y2K Matrix"
    assert item.unplayable is Unplayable.NEEDS_BASE
    assert item.scene_base == "2777242023"
    assert "2777242023" in item.unplayable_reason
    assert "isn\u2019t installed" in item.unplayable_reason


@pytest.mark.parametrize("dependency", (None, "", "../2777242023", True, ["2777242023"]))
def test_a_preset_that_names_no_base_is_listed_needing_one(
    tmp_path: Path, dependency: object
) -> None:
    content = _content(tmp_path)
    _web(content)
    _preset(content, {"arcs": True}, dependency=dependency)

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is Unplayable.NEEDS_BASE and item.scene_base == ""


def test_a_numeric_dependency_names_its_base_too(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _base(content)
    _preset(content, {"arcs": True}, dependency=2777242023)

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is None and item.scene_base == "2777242023"


@pytest.mark.parametrize(
    ("project", "word"),
    (
        ({"type": "Hologram", "title": "Future", "file": "x"}, "hologram"),
        ({"title": "No type"}, "unknown"),
        ({"type": "x" * 200, "title": "Long", "file": "x"}, "unknown"),
    ),
)
def test_a_type_the_engine_cant_run_is_listed_and_said(
    tmp_path: Path, project: dict[str, object], word: str
) -> None:
    directory = _install(_content(tmp_path), "4242", project)

    (item,) = _listed(tmp_path).values()

    assert item.kind is Kind.SCENE and item.path == directory
    assert item.workshop_type == word
    assert item.unplayable is Unplayable.RUNTIME
    assert "linux-wallpaperengine can\u2019t play" in item.unplayable_reason


def test_a_preset_of_a_wallpaper_the_engine_cant_run_is_unsupported_too(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _install(content, "2777242023", {"type": "application", "title": "App", "file": "a.exe"})
    _preset(content, {"arcs": True})

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is Unplayable.RUNTIME
    assert "App" in item.unplayable_reason


def test_a_supported_type_without_what_the_engine_requires_is_not_played(tmp_path: Path) -> None:
    _install(_content(tmp_path), "5150", {"type": "web", "title": "No file"})

    (item,) = _listed(tmp_path).values()

    assert item.workshop_type == "web" and item.unplayable is Unplayable.MALFORMED


@pytest.mark.parametrize("file", ("v/a.mp4", "gone.mp4", ""))
def test_a_video_whose_file_the_app_cant_open_is_not_listed_as_before(
    tmp_path: Path, file: str
) -> None:
    """Deleting a Workshop video's file is how it leaves the library."""
    _install(_content(tmp_path), "6060", {"type": "video", "title": "Nested", "file": file})

    assert _listed(tmp_path) == {}


# -- executable wallpapers are never run (owner decision, security) -------------


def _application(content: Path, workshop_id: str = "2777242023") -> Path:
    directory = _install(
        content,
        workshop_id,
        {"type": "Application", "title": "Rainmeter", "file": "run.exe", "preview": "p.jpg"},
        file="run.exe",
    )
    (directory / "p.jpg").write_bytes(b"\xff\xd8\xff")
    return directory


def test_an_application_is_listed_badged_and_never_run(tmp_path: Path) -> None:
    directory = _application(_content(tmp_path))

    (item,) = _listed(tmp_path).values()

    assert item.kind is Kind.SCENE and item.path == directory
    assert item.workshop_type == "application" and item.title == "Rainmeter"
    assert item.unplayable is Unplayable.RUNTIME, "counted with the unsupported ones"
    assert "never runs programs" in item.unplayable_reason


def test_a_preset_of_an_application_is_never_run_either(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _application(content)
    _preset(content, {"arcs": True})

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is Unplayable.RUNTIME and item.scene_properties == ()
    assert "Rainmeter" in item.unplayable_reason
    assert "never runs programs" in item.unplayable_reason


def test_the_deny_list_wins_even_if_the_engine_learns_the_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _content(tmp_path)
    _application(content)
    _preset(content, {"arcs": True})
    monkeypatch.setattr(workshop, "SUPPORTED_TYPES", workshop.SUPPORTED_TYPES | {"application"})

    found = _listed(tmp_path)

    assert "application" in workshop.SUPPORTED_TYPES and "application" in workshop.NEVER_RUN
    assert found["2777242023"].unplayable is Unplayable.RUNTIME
    assert found["2946165131"].unplayable is Unplayable.RUNTIME


@pytest.mark.parametrize(
    ("kind", "file", "runs"),
    (
        ("web", "index.html", True),
        ("web", "INDEX.HTM", True),
        ("web", "index.sh", False),
        ("web", "index", False),
        ("scene", "scene.json", True),
        ("scene", "scene.elf", False),
    ),
)
def test_a_file_that_isnt_what_its_type_names_is_never_run(
    tmp_path: Path, kind: str, file: str, runs: bool
) -> None:
    """Judged by name: nothing is read or run to decide."""
    content = _content(tmp_path)
    directory = _install(content, "2777242023", {"type": kind, "title": "T", "file": file})
    (directory / file).write_bytes(b"\x7fELF")
    _preset(content, {"arcs": True})

    found = _listed(tmp_path)

    for item in found.values():
        assert item.unplayable is (None if runs else Unplayable.RUNTIME), item


def test_a_preset_with_more_values_than_are_passed_on_is_not_played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _content(tmp_path)
    _base(content)
    _preset(content, {"arcs": True, "arcsGeneralRadius": 1, "theme": "dark"})
    monkeypatch.setattr(workshop, "MAX_PROPERTIES", 2)

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is Unplayable.MALFORMED and item.scene_properties == ()
    assert "at most 2" in item.unplayable_reason


def test_a_folder_that_holds_no_wallpaper_is_said_and_never_blocks_the_compiler(
    tmp_path: Path,
) -> None:
    from wall_in_one.library import scan
    from wall_in_one.library.model import UnseenKind

    content = _content(tmp_path)
    empty = content / "9999999999"
    empty.mkdir(parents=True)
    broken = content / "8888888888"
    broken.mkdir()
    (broken / "project.json").write_text("{not json", encoding="utf-8")
    listed = content / "7777777777"
    listed.mkdir()
    (listed / "project.json").write_text("[1, 2]", encoding="utf-8")
    library_root = tmp_path / "wallpapers"
    library_root.mkdir()

    library = scan.scan(
        (library_root,), include_workshop=True, workshop_roots=(tmp_path,), records={}
    )

    assert library.items == ()
    said = {place.path: place for place in library.unseen}
    assert set(said) == {empty, broken, listed}
    assert all(place.kind is UnseenKind.NOT_A_WALLPAPER for place in said.values())
    assert not any(place.kind.could_not_look for place in said.values())
    assert said[empty].note == f"{empty}: {workshop.NO_PROJECT}"
    assert said[broken].note == f"{broken}: {workshop.NOT_A_PROJECT}"
    assert library.skipped == (), "looked at: it isn't 'couldn't look'"


# -- what the Scenes view says about them ------------------------------------


def test_the_unsupported_notice_counts_in_words_and_is_empty_at_zero() -> None:
    from wall_in_one.ui.next.catalog import unsupported_notice

    assert unsupported_notice(0) == ""
    assert unsupported_notice(1) == (
        "1 Wallpaper Engine wallpaper needs a runtime linux-wallpaperengine "
        "doesn\u2019t support yet"
    )
    assert unsupported_notice(2) == (
        "2 Wallpaper Engine wallpapers need a runtime linux-wallpaperengine "
        "doesn\u2019t support yet"
    )


def test_the_notice_names_ten_by_title_then_how_many_more() -> None:
    from wall_in_one.ui.next.catalog import unsupported_details

    assert unsupported_details(()) == ""
    titles = tuple(f"Item {index:02d}" for index in range(12))
    details = unsupported_details(titles)
    assert details.startswith("Listed, never played: Item 00, Item 01,")
    assert "Item 09 and 2 more." in details and "Item 10" not in details


def test_only_what_no_renderer_runs_is_counted(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _web(content)
    _install(content, "100", {"type": "application", "title": "zebra", "file": "x.exe"})
    _install(content, "101", {"type": "hologram", "title": "Apple", "file": "x"})
    _preset(content, {"arcs": True}, dependency="999")  # needs its base: not counted

    assert workshop.unsupported_titles(_listed(tmp_path).values()) == ("Apple", "zebra")


# -- one item never costs the scan (review of r11, finding 1) -------------------

#: More digits than any float holds: `math.isfinite` on it raises OverflowError.
HUGE = "1" + "0" * 400


def _raw(content: Path, workshop_id: str, text: str, file: str = "") -> Path:
    directory = content / workshop_id
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(text, encoding="utf-8")
    if file:
        (directory / file).write_bytes(b"\x00")
    return directory


def test_a_huge_integer_in_a_combo_option_costs_nothing(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _install(content, "200", {"type": "video", "title": "Innocent", "file": "v.mp4"}, file="v.mp4")
    _raw(
        content,
        "201",
        '{"title":"x","type":"scene","file":"scene.json","general":{"properties":'
        '{"k":{"type":"combo","value":1,"options":[{"value":' + HUGE + ',"label":"a"},'
        '{"value":3,"label":"b"},{"value":2147483648,"label":"c"}]}}}}',
    )

    found = workshop.scan((tmp_path,), include_defaults=False)

    assert {item.id for item in found} == {"200", "201"}
    (scene,) = (item for item in found if item.id == "201")
    assert scene.declared == (workshop.Declared("k", "combo", frozenset({"3"})),)


def test_a_huge_integer_in_a_presets_values_costs_nothing(tmp_path: Path) -> None:
    content = _content(tmp_path)
    _install(content, "200", {"type": "video", "title": "Innocent", "file": "v.mp4"}, file="v.mp4")
    _install(
        content,
        "201",
        {
            "title": "base",
            "type": "scene",
            "file": "scene.json",
            "general": {
                "properties": {
                    "s": {"type": "slider", "value": 1},
                    "k": {"type": "combo", "value": 1, "options": [{"value": 1, "label": "a"}]},
                    "t": {"type": "slider", "value": 1},
                }
            },
        },
    )
    _raw(
        content,
        "202",
        '{"title":"p","preset":{"s":' + HUGE + ',"k":' + HUGE + ',"t":7},"dependency":"201"}',
    )

    found = _listed(tmp_path)

    assert {"201", "202"} <= set(found)
    preset = found["202"]
    assert preset.unplayable is None
    assert preset.scene_properties == (("t", "7"),), "the huge ones are left out"
    assert any(item.title == "Innocent" for item in found.values())


def test_whatever_reading_one_item_raises_is_said_and_the_rest_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.library.model import UnseenKind

    content = _content(tmp_path)
    _install(content, "200", {"type": "video", "title": "Innocent", "file": "v.mp4"}, file="v.mp4")
    broken = _install(content, "201", {"type": "scene", "title": "Boom", "file": "scene.json"})
    real = workshop._declarations

    def boom(general: object) -> tuple[workshop.Declared, ...]:
        if isinstance(general, dict) and general.get("boom"):
            raise RuntimeError("nothing foresaw this")
        return real(general)

    monkeypatch.setattr(workshop, "_declarations", boom)
    (broken / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Boom", "file": "scene.json", "general": {"boom": 1}})
    )
    said: list[tuple[Path, UnseenKind, str]] = []

    found = workshop.scan(
        (tmp_path,), include_defaults=False, on_unseen=lambda p, k, w: said.append((p, k, w))
    )

    assert [item.id for item in found] == ["200"]
    assert said == [
        (broken, UnseenKind.NOT_A_WALLPAPER, "its project.json could not be read (RuntimeError)")
    ]


# -- a preset of a web wallpaper plays as its base (review of r11, finding 3) -----


def test_a_preset_of_a_web_wallpaper_plays_as_its_base_and_says_so(tmp_path: Path) -> None:
    """linux-wallpaperengine hands a page no properties: nothing is passed, and it says so."""
    content = _content(tmp_path)
    web = _web(content)
    preset = _preset(content, {"arcs": False, "arcsGeneralRadius": 350})

    item = _listed(tmp_path)["2946165131"]

    assert item.unplayable is None and item.scene_base == "2777242023"
    assert item.scene_properties == (), "none: the page would never see them"
    assert item.scene_note == "plays as Electric Eye; its settings aren\u2019t applied"
    assert item.preview == web / "preview.jpg", "its still and colours match the screen"
    assert item.title == "Y2K Matrix" and item.path == preset


# -- a value std::stof refuses is never written (review of r11, finding 4) --------


@pytest.mark.parametrize(
    ("value", "written"),
    (
        (1e-300, "0.0"),
        (1.5e-39, "0.0"),
        (1e-38, "0.0"),
        (-1e-38, "0.0"),
        (1.2e-38, "1.2e-38"),
        (0.0, "0.0"),
        (-2.5, "-2.5"),
        (1e9, "1000000000.0"),
    ),
)
def test_a_slider_value_is_one_stof_parses(value: float, written: str) -> None:
    """`std::stof` throws on 0 < |v| < FLT_MIN (1e-300, 1.5e-39, 1e-38; checked
    against libstdc++), and the engine then exits: such a value is written as 0."""
    slider = workshop.Declared("s", "slider")
    assert workshop.property_value(slider, value) == written


# -- types and files are judged as the engine judges them (review of r11, finding 5) --


@pytest.mark.parametrize(
    ("kind", "file", "plays"),
    (
        ("scene", "scene.json", True),
        ("Scene", "scene.json", True),  # tolower, as ProjectParser.cpp:33
        ("scene", "scene.JSON", True),
        ("\u017fcene", "scene.json", False),  # long s: casefold() says "scene", tolower doesn't
        (" scene", "scene.json", False),  # nothing is stripped
        ("scene ", "scene.json", False),
        ("scene", " scene.json", False),  # the engine opens the file exactly as written
        ("scene", "scene.json ", False),
        ("Application", "app.exe", False),
        (" application ", "app.exe", False),
    ),
)
def test_a_type_or_file_the_engine_refuses_never_plays(
    tmp_path: Path, kind: str, file: str, plays: bool
) -> None:
    _install(_content(tmp_path), "1647046763", {"type": kind, "title": "T", "file": file})

    (item,) = _listed(tmp_path).values()

    assert (item.unplayable is None) is plays, item


# -- a preset of a video it can't open needs that base (review of r11, finding 6) ---


@pytest.mark.parametrize("video", ("gone.mp4", "link.mp4"))
def test_a_preset_of_a_video_whose_file_isnt_there_needs_its_base(
    tmp_path: Path, video: str
) -> None:
    content = _content(tmp_path)
    base = _install(content, "160", {"type": "video", "title": "Clip", "file": video})
    if video == "link.mp4":
        (base / video).symlink_to("/dev/zero")
    _preset(content, {"speed": 2}, dependency="160")

    found = _listed(tmp_path)

    assert set(found) == {"2946165131"}, "the video itself stays unlisted, as before"
    preset = found["2946165131"]
    assert preset.unplayable is Unplayable.NEEDS_BASE
    assert "Clip" in preset.unplayable_reason and "isn\u2019t there" in preset.unplayable_reason


# -- only folders named by a Workshop id are listed (review of r11, finding 7) ------


def test_a_folder_not_named_by_an_ascii_workshop_id_is_said_and_not_listed(
    tmp_path: Path,
) -> None:
    from wall_in_one.library import scan
    from wall_in_one.library.model import UnseenKind

    content = _content(tmp_path)
    project: dict[str, object] = {"type": "web", "title": "T", "file": "index.html"}
    for name in ("\u0661\u0662\u0663", "abc", "12a", "123"):
        _install(content, name, project, file="index.html")
    said: list[tuple[str, UnseenKind, str]] = []

    items = scan.workshop_items((tmp_path,), on_unseen=lambda p, k, w: said.append((p.name, k, w)))

    assert [item.scene for item in items] == ["123"]
    assert sorted(said) == sorted(
        (name, UnseenKind.NOT_A_WALLPAPER, workshop.NOT_AN_ID)
        for name in ("\u0661\u0662\u0663", "abc", "12a")
    )


# -- nits from the review of r11 (finding 9) --------------------------------------


def test_a_preset_object_does_not_override_a_declared_type_without_a_dependency(
    tmp_path: Path,
) -> None:
    _install(
        _content(tmp_path),
        "1647046763",
        {"type": "scene", "title": "T", "file": "scene.json", "preset": {"bloom": True}},
    )

    (item,) = _listed(tmp_path).values()

    assert item.workshop_type == "scene" and item.unplayable is None
    assert item.scene_base == ""


def test_an_application_with_a_preset_and_a_dependency_is_still_never_run(
    tmp_path: Path,
) -> None:
    content = _content(tmp_path)
    _base(content)
    _install(
        content,
        "143",
        {
            "type": "application",
            "title": "Disguised",
            "file": "a.exe",
            "preset": {"arcs": True},
            "dependency": "2777242023",
        },
    )

    item = _listed(tmp_path)["143"]

    assert item.unplayable is Unplayable.RUNTIME and item.runs_program
    assert item.scene_base == "" and item.scene_properties == ()
    assert "never runs programs" in item.unplayable_reason


def test_only_an_executable_among_them_says_executables_are_never_run(tmp_path: Path) -> None:
    from wall_in_one.ui.next.catalog import unsupported_details

    content = _content(tmp_path)
    _install(content, "101", {"type": "hologram", "title": "Apple", "file": "x"})
    hologram_only = list(_listed(tmp_path).values())
    assert not workshop.runs_a_program(hologram_only)
    _install(content, "100", {"type": "application", "title": "Zebra", "file": "x.exe"})
    _preset(content, {"a": 1}, dependency="100")
    both = list(_listed(tmp_path).values())
    assert workshop.runs_a_program(both)
    assert sorted(item.title for item in both if item.runs_program) == ["Y2K Matrix", "Zebra"]

    assert unsupported_details(("Apple",)) == "Listed, never played: Apple."
    assert unsupported_details(("Apple", "Zebra"), executable=True) == (
        "Listed, never played: Apple, Zebra. Executable wallpapers are never run, for security."
    )
