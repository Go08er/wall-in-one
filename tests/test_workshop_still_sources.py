"""A Workshop item's still comes only from Wall-in-One's own work or a person's pick.

The owner's rule (docs/wallpaper-engine.md, "Stills"): never from a file the
Workshop item brought. So a web page or a preset gets no automatic still at
all, and a picture beside a Workshop video is not its still either. The upgrade
case is the other half: 0.4.1 drew a still from the preview for those two kinds
and left it in Automatic Stills as ``<id>.png``; it is left on disk and not
used, a picked still still is, and nothing raises while the document that named
it is rebuilt.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one import runtime_config as rc
from wall_in_one.library import pairing, pairings, playlists, scan, workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class _Tree:
    def __init__(self, tmp_path: Path) -> None:
        self.steam = tmp_path / "steam"
        self.content = self.steam / "steamapps" / "workshop" / "content" / workshop.APP_ID
        self.root = tmp_path / "library"
        self.automatic = pairing.still_directory(self.root)
        self.automatic.mkdir(parents=True)
        scene = {"type": "scene", "file": "scene.json"}
        self.put("200", {**scene, "title": "Scene", "preview": "p.png"})
        self.put("300", {"type": "web", "title": "Web", "file": "index.html", "preview": "p.png"})
        self.put("400", {"title": "Preset", "dependency": "300", "preset": {"a": 1}})
        self.put("210", {"type": "video", "title": "Video", "file": "clip.mp4", "preview": "p.png"})
        # The author's pictures: a preview beside everything, and one named like the video.
        (self.content / "210" / "clip.png").write_bytes(PNG)
        # What 0.4.1 made from the previews, plus the true scene's engine capture.
        for name in ("200", "300", "400"):
            (self.automatic / f"{name}.png").write_bytes(PNG)
        self.mine = self.root / "mine.png"
        self.mine.write_bytes(PNG)

    def put(self, wid: str, project: dict[str, object]) -> None:
        folder = self.content / wid
        folder.mkdir(parents=True)
        (folder / "project.json").write_text(json.dumps(project))
        for name in ("scene.json", "scene.pkg", "clip.mp4", "index.html", "p.png"):
            (folder / name).write_bytes(PNG if name.endswith(".png") else b"x")

    def scan(self, records: dict[str, pairings.Pairing] | None = None) -> dict[str, MediaItem]:
        library = scan.scan(
            [self.root], records or {}, include_workshop=True, workshop_roots=(self.steam,)
        )
        return {item.scene or item.path.name: item for item in library.items}

    def session(
        self,
        store: pairings.Store | None = None,
        *,
        playlist: playlists.Playlist | None = None,
        settings: config.Settings | None = None,
    ) -> tuple[config.Settings, Session]:
        store = store or pairings.Store({})
        settings = settings or config.Settings(
            roots=(self.root,),
            allow_web_wallpapers=True,
            active_playlist=playlist.id if playlist else "",
        )
        held: list[Session] = []

        def scanner(_roots: object) -> object:
            # The session keeps its own copy of the store: read that one.
            records = held[0].pairings.records if held else store.records
            return scan.scan(
                [self.root],
                records,
                include_workshop=True,
                workshop_roots=(self.steam,),
                previews=settings.preview_stills,
            )

        session = Session(
            settings,
            scanner=scanner,  # type: ignore[arg-type]
            pairing_store=store,
            playlist_store=playlists.Store({playlist.id: playlist} if playlist else {}),
        )
        held.append(session)
        session.refresh()
        return settings, session


@pytest.fixture
def tree(tmp_path: Path) -> _Tree:
    return _Tree(tmp_path)


def _scene_ids(document: str) -> set[str]:
    decoded = tomllib.loads(document)
    return {
        str(entry["scene_id"])
        for playlist in decoded.get("playlists", [])
        for entry in playlist["entries"]
        if "scene_id" in entry
    }


def test_a_picture_beside_a_workshop_video_is_not_its_still(tree: _Tree) -> None:
    video = next(item for item in tree.scan().values() if item.kind is Kind.VIDEO)
    assert video.path.with_suffix(".png").is_file()
    assert video.paired_still is None
    assert pairing.find_still(video.path, (tree.root,), labelled_workshop=True) is None
    # Place alone says so too, whatever the scan labelled it.
    assert pairing.find_still(video.path, (tree.root,), labelled_workshop=False) is None


def test_a_sibling_picture_beside_a_copied_in_workshop_item_is_not_its_still(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "library" / "copied"
    folder.mkdir(parents=True)
    video, sibling = folder / "clip.mp4", folder / "clip-still.png"
    video.write_bytes(b"x")
    sibling.write_bytes(PNG)
    root = tmp_path / "library"
    assert pairing.find_still(video, (root,), labelled_workshop=False) == sibling
    (folder / "project.json").write_text("{}", encoding="utf-8")
    assert pairing.find_still(video, (root,), labelled_workshop=False) is None


def test_an_old_preview_made_still_is_not_used_for_a_web_page_or_preset(tree: _Tree) -> None:
    items = tree.scan()
    assert items["300"].paired_still is None and items["400"].paired_still is None
    assert items["200"].paired_still == tree.automatic / "200.png", "a true scene's capture stays"
    for old in ("300", "400"):
        assert (tree.automatic / f"{old}.png").is_file(), "nothing deletes the old file"


def test_a_picked_still_is_used_for_a_web_page_or_preset(tree: _Tree) -> None:
    web = tree.scan()["300"]
    records = {
        pairings.Identity.of(web).key: pairings.Pairing(
            identity=pairings.Identity.of(web), still=tree.mine, customized=True
        )
    }
    assert tree.scan(records)["300"].paired_still == tree.mine


def test_the_old_document_is_rebuilt_without_the_entries_that_lost_their_still(
    tree: _Tree,
) -> None:
    """What the upgrade shows: the item plays nothing until a still is picked."""
    settings, session = tree.session()
    document = rc.render(settings, session)
    assert _scene_ids(document) == {"200"}
    assert str(tree.automatic / "300.png") not in document
    assert str(tree.automatic / "400.png") not in document
    web = next(item for item in session.library.items if item.scene == "300")
    assert rc.entry_playability(web, session) == rc.Playability(
        False,
        False,
        "it has no still, and gets none of its own while previews are off: pick one",
    )


def test_a_playlist_of_only_such_items_is_left_out_not_raised(tree: _Tree) -> None:
    web_entry = playlists.Entry(id="e", source=str(tree.content / "300"))
    only = playlists.Playlist(id="web", name="Web", entries=(web_entry,))
    settings, session = tree.session(playlist=only)
    compiled = rc.compiled(settings, session)
    assert any(left.why == rc.NOTHING_TO_PLAY for left in compiled.left_out)


def test_a_picked_still_makes_the_entry_compile_again(tree: _Tree) -> None:
    _settings, session = tree.session()
    web = next(item for item in session.library.items if item.scene == "300")
    session.pairings.choose_still(web, tree.mine)
    session.refresh()
    settings = session.settings
    document = rc.render(settings, session)
    assert "300" in _scene_ids(document)
    assert f'still = "{tree.mine}"' in document


def test_a_record_naming_the_old_preview_made_still_does_not_bring_it_back(tree: _Tree) -> None:
    """A record can hold `<id>.png` (a palette change on 0.4.1's item, or a pick of it)."""
    web = tree.scan()["300"]
    old = tree.automatic / "300.png"
    identity = pairings.Identity.of(web)
    records = {
        identity.key: pairings.Pairing(identity=identity, still=old, customized=True),
    }
    assert tree.scan(records)["300"].paired_still is None
    assert pairings.resolve(web, (tree.root,), records).still is None
    session = tree.session(pairings.Store(records))[1]
    assert session.pairings.resolve_accepted(web, session.library).still is None
    assert old.is_file()
    assert old not in {item.path for item in session.library.reusable_stills}


def test_the_pages_say_there_is_no_automatic_still_and_offer_no_regenerate(tree: _Tree) -> None:
    from wall_in_one.ui.next import real_state

    items = tree.scan()
    web = items["300"]
    note = real_state._still_note(web, None, False)
    assert "choose one by hand" in note and "preview" not in note
    assert real_state._still_note(items["200"], None, False) == "Captured from the scene"


# -- f7 W1: no still directory an item's author could have shipped is looked in ----------------


def _steam_scene(steam: Path, wid: str) -> Path:
    folder = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / wid
    folder.mkdir(parents=True)
    (folder / "project.json").write_text(
        json.dumps({"type": "scene", "title": wid, "file": "scene.json"})
    )
    for name in ("scene.json", "scene.pkg"):
        (folder / name).write_bytes(b"x")
    return folder


def _authors_root(tmp_path: Path, steam: Path, place: str) -> Path:
    """A library folder an item's author writes in: an item copied in by hand, one of Steam's,
    or a folder inside one of Steam's (with no ``project.json`` of its own)."""
    if place == "steam-item":
        return _steam_scene(steam, "300")
    if place == "inside-a-steam-item":
        inside = _steam_scene(steam, "300") / "assets"
        inside.mkdir()
        return inside
    root = tmp_path / "copied-in"
    root.mkdir()
    (root / "project.json").write_text(json.dumps({"type": "video", "file": "v.mp4"}))
    return root


@pytest.mark.parametrize("planted", ["file", "link"])
@pytest.mark.parametrize("place", ["copied-in-item", "steam-item", "inside-a-steam-item"])
def test_a_still_an_items_author_could_have_shipped_is_not_paired(
    tmp_path: Path, place: str, planted: str
) -> None:
    """``<root>/Wall-in-One/Automatic Stills`` was looked in under every library folder,
    one that is an item folder too, so the item's author could ship any scene's still, or
    any video's, or a link out to a file of yours (on 86840ed). It is not looked in now, and
    nothing is captured there."""
    steam = tmp_path / "steam"
    _steam_scene(steam, "200")
    root = _authors_root(tmp_path, steam, place)
    mine = tmp_path / "mine"
    mine.mkdir()
    video = mine / "clip.mp4"
    video.write_bytes(b"x")
    yours = tmp_path / "yours.png"
    yours.write_bytes(PNG)
    shipped = pairing.still_directory(root)
    shipped.mkdir(parents=True)
    for name in ("200.png", f"{pairing.automatic_still_stem(video)}.png"):
        if planted == "link":
            (shipped / name).symlink_to(yours)
        else:
            (shipped / name).write_bytes(PNG)

    library = scan.scan([root], {}, include_workshop=True, workshop_roots=(steam,))
    scene = next(item for item in library.items if item.scene == "200")
    assert scene.paired_still is None
    assert pairing.find_still(video, roots=(mine, root), labelled_workshop=False) is None
    assert pairing.capture_root([root]) is None


def test_a_library_folder_inside_an_item_folder_keeps_its_own_stills(tmp_path: Path) -> None:
    """R42-12's design: a library folder of someone's own inside an item folder lists its own
    files, and its still directory is theirs as well. Only the item folder itself is not."""
    steam = tmp_path / "steam"
    _steam_scene(steam, "200")
    item = _authors_root(tmp_path, steam, "copied-in-item")
    root = item / "mine"
    root.mkdir()
    capture = pairing.still_directory(root) / "200.png"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(PNG)

    library = scan.scan([root], {}, include_workshop=True, workshop_roots=(steam,))
    assert next(item for item in library.items if item.scene == "200").paired_still == capture
    assert pairing.capture_root([item, root]) == root


def test_the_apps_capture_in_steams_own_content_folder_still_pairs(tmp_path: Path) -> None:
    """A library folder that is Steam's ``431960`` folder itself: its still directory sits
    beside the items (``431960/<id>/``), where none of them writes, so it is the app's."""
    steam = tmp_path / "steam"
    _steam_scene(steam, "200")
    root = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID
    capture = pairing.still_directory(root) / "200.png"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(PNG)

    library = scan.scan([root], {}, include_workshop=True, workshop_roots=(steam,))
    assert next(item for item in library.items if item.scene == "200").paired_still == capture
    assert pairing.capture_root([root]) == root


def test_captures_go_to_the_first_root_no_items_author_can_write(tmp_path: Path) -> None:
    """One made under another would never pair, so it would be made again after every scan."""
    steam = tmp_path / "steam"
    copied = _authors_root(tmp_path, steam, "copied-in-item")
    steams = _authors_root(tmp_path, steam, "steam-item")
    mine = tmp_path / "mine"
    mine.mkdir()
    assert pairing.capture_root([copied, steams, mine]) == mine
    assert pairing.capture_root([mine, copied]) == mine
    assert pairing.capture_root([copied, steams]) is None


def test_a_removal_takes_nothing_from_a_still_directory_nothing_of_ours_is_in(
    tmp_path: Path,
) -> None:
    """A scene's removal took ``<id>.png`` out of every root's still directory, an item
    folder's too, where the author's file is: only one the app writes in is looked in now."""
    from wall_in_one.library import manage

    steam = tmp_path / "steam"
    folder = _steam_scene(steam, "200")
    copied = _authors_root(tmp_path, steam, "copied-in-item")
    mine = tmp_path / "mine"
    mine.mkdir()
    scene = MediaItem(path=folder, kind=Kind.SCENE, size=0, mtime=0, scene="200")

    found = manage.pairing_artifact_paths(scene, (copied, mine))
    assert pairing.still_directory(mine) / "200.png" in found
    assert not any(path.is_relative_to(copied) for path in found), found
