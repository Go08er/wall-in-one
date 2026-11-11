"""`web_wallpaper_previews`: a web page's or a preset's own preview as its still, as a sub-option.

Owner decision: "a separate toggle to use the built-in preview, only enablable
when web is enabled, like a sub-option". The effective value is
``allow_web_wallpapers`` AND the setting (`config.Settings.preview_stills`);
only that is ever asked (`workshop.preview_still`, `workshop.no_automatic_still`).
A profile from 0.4.1 that allowed web pages keeps their preview stills.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.test_stills import _workshop_scene, root  # noqa: F401
from tests.test_workshop_still_sources import PNG, _Tree
from wall_in_one import config
from wall_in_one import runtime_config as rc
from wall_in_one.library import pairing, pairings, scan, stills
from wall_in_one.wallpaper import scenes

KEY = "web_wallpaper_previews"


def _load(text: str) -> config.Settings:
    return config.Settings.from_mapping(tomllib.loads(text))


# -- the setting and its upgrade default ------------------------------------------------------


def test_a_0_4_1_profile_that_allowed_web_pages_keeps_their_preview_stills() -> None:
    settings = _load("roots = []\nallow_web_wallpapers = true\n")  # 0.4.1: no such key
    assert settings.web_wallpaper_previews and settings.preview_stills


@pytest.mark.parametrize("document", ["roots = []\n", "roots = []\nallow_web_wallpapers = false\n"])
def test_a_profile_with_web_off_has_it_off(document: str) -> None:
    settings = _load(document)
    assert not settings.web_wallpaper_previews and not settings.preview_stills


def test_a_new_profile_has_it_off() -> None:
    assert config.Settings() == config.Settings(web_wallpaper_previews=False)
    assert not config.Settings().preview_stills
    assert not config.Settings(allow_web_wallpapers=True).preview_stills


def test_a_saved_value_is_kept_and_applies_only_while_web_is_on() -> None:
    assert not _load("allow_web_wallpapers = true\nweb_wallpaper_previews = false\n").preview_stills
    off = _load("allow_web_wallpapers = false\nweb_wallpaper_previews = true\n")
    assert off.web_wallpaper_previews and not off.preview_stills  # kept; does not apply
    assert config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=True).preview_stills


@pytest.mark.parametrize("web", [True, False])
@pytest.mark.parametrize("previews", [True, False])
def test_every_combination_reads_back_as_saved(web: bool, previews: bool) -> None:
    chosen = config.Settings(allow_web_wallpapers=web, web_wallpaper_previews=previews)
    assert _load(chosen.to_toml()) == chosen


@pytest.mark.parametrize(
    ("web", "previews", "written"),
    [(True, True, False), (False, False, False), (True, False, True), (False, True, True)],
)
def test_the_key_is_written_only_when_its_absence_would_mean_something_else(
    web: bool, previews: bool, written: bool
) -> None:
    """So an untouched 0.4.1 profile keeps its bytes, and a 0.4.1 build is not troubled."""
    text = config.Settings(allow_web_wallpapers=web, web_wallpaper_previews=previews).to_toml()
    assert (KEY in tomllib.loads(text)) is written
    # What 0.4.1 would call unknown (it makes settings read-only for any such key).
    known_to_0_4_1 = config.KNOWN_KEYS - {KEY}
    assert set(tomllib.loads(text)) - known_to_0_4_1 == (set() if not written else {KEY})


def test_saving_a_0_4_1_document_does_not_add_the_key() -> None:
    # What 0.4.1 wrote for a profile that allowed web pages: no such key.
    original = "roots = []\nallow_web_wallpapers = true\n"
    saved = _load(original).to_toml()
    assert KEY not in saved and "allow_web_wallpapers = true" in saved
    assert _load(saved).to_toml() == saved


def test_a_non_boolean_value_is_refused_by_the_headless_reader(tmp_path: Path) -> None:
    target = tmp_path / "settings.toml"
    target.write_text('roots = []\nweb_wallpaper_previews = "yes"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="web_wallpaper_previews must be a boolean"):
        config.load_strict(target)


# -- what each combination does to a web page or a preset ------------------------------------


@pytest.mark.parametrize("kind", ["web", "preset"])
def test_ensure_draws_the_still_from_the_preview_only_when_the_effective_setting_is_on(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,  # noqa: F811
    kind: str,
) -> None:
    item = _workshop_scene(root, "2777242023", kind, "preview.jpg")
    monkeypatch.setattr(scenes, "is_available", lambda: False)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda: None)
    target = pairing.still_directory(root) / "2777242023.png"
    cases = {
        "off": config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=False),
        "web off": config.Settings(allow_web_wallpapers=False, web_wallpaper_previews=True),
        "on": config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=True),
    }
    for name in ("off", "web off"):
        assert stills.ensure(item, root, previews=cases[name].preview_stills) is None, name
        assert not target.exists(), name
    assert stills.ensure(item, root, previews=cases["on"].preview_stills) == target
    assert target.is_file()


def _scanned(tree: _Tree, settings: config.Settings) -> dict[str, object]:
    library = scan.scan(
        [tree.root],
        {},
        include_workshop=True,
        workshop_roots=(tree.steam,),
        previews=settings.preview_stills,
    )
    return {item.scene: item for item in library.items}


def test_the_old_preview_made_still_counts_again_only_when_the_effective_setting_is_on(
    tmp_path: Path,
) -> None:
    tree = _Tree(tmp_path)
    old = tree.automatic / "300.png"
    for settings, used in (
        (config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=True), True),
        (config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=False), False),
        (config.Settings(allow_web_wallpapers=False, web_wallpaper_previews=True), False),
    ):
        items = _scanned(tree, settings)
        assert (items["300"].paired_still == old) is used, settings  # type: ignore[attr-defined]
        assert (items["400"].paired_still == tree.automatic / "400.png") is used  # type: ignore[attr-defined]
        assert items["200"].paired_still == tree.automatic / "200.png"  # type: ignore[attr-defined]
    assert old.is_file() and PNG


def test_the_compiler_follows_the_same_answer(tmp_path: Path) -> None:
    tree = _Tree(tmp_path)
    shown: dict[bool, set[str]] = {}
    for previews in (True, False):
        settings = config.Settings(
            roots=(tree.root,), allow_web_wallpapers=True, web_wallpaper_previews=previews
        )
        session = tree.session(settings=settings)[1]
        try:
            document = tomllib.loads(rc.render(settings, session))
        finally:
            session.shutdown()
        shown[previews] = {
            str(entry["scene_id"])
            for playlist in document.get("playlists", [])
            for entry in playlist["entries"]
            if "scene_id" in entry
        }
    assert shown[True] == {"200", "300", "400"}
    assert shown[False] == {"200"}


def test_a_picked_still_works_whatever_the_setting(tmp_path: Path) -> None:
    tree = _Tree(tmp_path)
    web = _scanned(tree, config.Settings())["300"]
    identity = pairings.Identity.of(web)  # type: ignore[arg-type]
    records = {identity.key: pairings.Pairing(identity=identity, still=tree.mine, customized=True)}
    for previews in (True, False):
        library = scan.scan(
            [tree.root],
            records,
            include_workshop=True,
            workshop_roots=(tree.steam,),
            previews=previews,
        )
        assert {i.scene: i for i in library.items}["300"].paired_still == tree.mine
