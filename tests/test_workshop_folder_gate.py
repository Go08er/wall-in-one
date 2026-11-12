"""Workshop content is untrusted: the root walk, and every decode of a file somebody wrote.

F1 of the 0.4.2 review: a library root that contains Steam's Workshop tree listed a
web wallpaper's `preview.gif` as local media (a video to the classifier), then
thumbnailed it and drew a still from it with ffmpeg, with web wallpapers off.
`workshop.folder_gate` decides, once per folder, by the place test every other
gate uses; and every ffmpeg that opens such a file is told what it may open
(`decoding.input_options`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_stills import make_video
from tests.test_thumbnails import _first_pixel, _made, _red
from wall_in_one import decoding, thumbnails
from wall_in_one.library import pairing, scan, stills, workshop
from wall_in_one.library.model import Kind, MediaItem


def _listed(root: Path) -> set[str]:
    return {str(item.path.relative_to(root)) for item in scan.scan([root], records={}).items}


def _folder(root: Path, name: str, project: object | None, files: tuple[str, ...]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    if project is not None:
        (folder / "project.json").write_text(
            project if isinstance(project, str) else json.dumps(project), encoding="utf-8"
        )
    for file in files:
        (folder / file).write_bytes(b"x")
    return folder


# -- the root walk ---------------------------------------------------------------------------


def test_steams_tree_inside_a_root_lists_no_web_wallpaper_file_as_local_media(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    item = "Games/steam/steamapps/workshop/content/431960/111"
    _folder(
        root,
        item,
        {"type": "web", "title": "W", "file": "index.html", "preview": "preview.gif"},
        ("index.html", "preview.gif", "extra.mp4", "preview.png"),
    )
    (root / "mine.png").write_bytes(b"x")
    assert _listed(root) == {"mine.png"}


def test_steams_tree_is_not_local_media_whatever_it_holds(tmp_path: Path) -> None:
    """Even a video item with no project.json at all: the Workshop scan lists the ones it lists."""
    root = tmp_path / "library"
    _folder(root, "steam/steamapps/workshop/content/431960/9", None, ("clip.mp4",))
    assert _listed(root) == set()


@pytest.mark.parametrize(
    "project",
    [
        {"type": "web", "title": "W", "file": "index.html", "preview": "preview.gif"},
        {"type": "application", "title": "A", "file": "run.exe", "preview": "preview.gif"},
        {"type": "mystery", "title": "M", "file": "clip.mp4", "preview": "preview.gif"},
        {"title": "no type at all", "file": "clip.mp4"},
        "{ not json",
        "[]",
    ],
)
def test_a_copied_in_item_folder_that_is_not_a_video_lists_nothing(
    tmp_path: Path, project: object
) -> None:
    root = tmp_path / "library"
    _folder(root, "copied", project, ("index.html", "preview.gif", "clip.mp4", "run.exe"))
    assert _listed(root) == set()


def test_a_copied_in_video_item_lists_its_declared_video_and_nothing_else(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _folder(
        root,
        "copied",
        {"type": "video", "title": "V", "file": "clip.mp4", "preview": "preview.gif"},
        ("clip.mp4", "preview.gif", "other.mp4", "preview.png"),
    )
    assert _listed(root) == {"copied/clip.mp4"}


def test_a_folder_without_a_project_json_is_listed_as_before(tmp_path: Path) -> None:
    root = tmp_path / "library"
    _folder(root, "plain", None, ("a.mp4", "b.gif", "c.png"))
    assert _listed(root) == {"plain/a.mp4", "plain/b.gif", "plain/c.png"}


def test_the_gate_is_the_place_test_every_other_gate_uses(tmp_path: Path) -> None:
    inside = tmp_path / "x/steamapps/workshop/content/431960/1"
    inside.mkdir(parents=True)
    other = tmp_path / "x/steamapps/workshop/content/999/1"
    other.mkdir(parents=True)
    assert workshop.folder_gate(inside) == frozenset()
    assert workshop.folder_gate(other) is None
    assert workshop.in_content_tree(inside / "preview.gif")
    assert workshop.is_workshop_content(inside / "preview.gif", labelled=False)


# -- ffmpeg: a playlist disguised as a picture or a video ---------------------------------------


def _disguised_concat(directory: Path, name: str) -> tuple[Path, Path]:
    """A script ffmpeg would follow to another local file, named like a picture or a video."""
    other = make_video(directory / "other.mp4")
    evil = directory / name
    evil.write_text("ffconcat version 1.0\nfile other.mp4\n", encoding="utf-8")
    return evil, other


def _frames(command: list[str], cwd: Path) -> int:
    done = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    return done.returncode


@pytest.mark.parametrize("name", ["preview.gif", "clip.mp4"])
def test_a_disguised_playlist_is_not_followed_by_the_still_maker_or_the_thumbnailer(
    tmp_path: Path, name: str
) -> None:
    evil, _other = _disguised_concat(tmp_path, name)
    # The premise: plain ffmpeg reads the other local file through it.
    plain = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(evil),
        "-frames:v",
        "1",
        str(tmp_path / "p.png"),
    ]
    assert _frames(plain, tmp_path) == 0, "without the options the playlist is followed"

    target = tmp_path / "still.png"
    assert _frames(stills._command(evil, target, 0.0), tmp_path) != 0
    assert not target.exists()

    item = MediaItem(path=evil, kind=Kind.VIDEO, size=1, mtime=0)
    destination = tmp_path / "thumb.png"
    with decoding.opened(evil) as source:
        assert _frames(thumbnails._command(item, source, destination), tmp_path) != 0
    assert not destination.exists()


def test_the_hardened_options_still_decode_a_real_video_and_gif(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4")
    gif = tmp_path / "a.gif"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=64x64:d=1:r=5", str(gif)],
        check=True,
    )
    for source in (video, gif):
        target = tmp_path / f"{source.name}.png"
        assert _frames(stills._command(source, target, 0.0), tmp_path) == 0, source
        assert target.is_file()
        thumb = tmp_path / f"{source.name}.thumb.png"
        item = MediaItem(path=source, kind=Kind.VIDEO, size=1, mtime=0)
        with decoding.opened(source) as opened:
            assert _frames(thumbnails._command(item, opened, thumb), tmp_path) == 0, source


@pytest.mark.parametrize("name", ["preview%01d.png", "preview.png"])
def test_a_declared_preview_is_thumbnailed_from_itself_whatever_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """R42-10: `_beside` accepts the regular file ``preview%01d.png`` declares, and ffmpeg's
    image2 read that name as a sequence: on 534a9da it decoded ``preview0.png`` instead, a
    link out of the item to a picture of somebody else's."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    steam = tmp_path / "steam"
    folder = steam / "steamapps/workshop/content" / workshop.APP_ID / "200"
    folder.mkdir(parents=True)
    declared = _made(folder / name, "red")
    outside = _made(tmp_path / "outside.png", "blue")
    (folder / "preview0.png").symlink_to(outside)
    (folder / "scene.json").write_text("{}", encoding="utf-8")
    (folder / "project.json").write_text(
        json.dumps({"type": "scene", "file": "scene.json", "preview": name}), encoding="utf-8"
    )
    library = tmp_path / "library"
    library.mkdir()
    found = scan.scan((library,), {}, include_workshop=True, workshop_roots=(steam,))
    item = next(item for item in found.items if item.scene == "200")
    assert item.preview == declared and item.paired_still is None

    assert _red(_first_pixel(thumbnails.generate(item, allow_web=True, force=True)))


def test_a_downloaded_playlist_handed_over_stdin_is_not_decoded(tmp_path: Path) -> None:
    _disguised_concat(tmp_path, "unused.gif")
    data = b"ffconcat version 1.0\nfile /etc/hostname\n"
    assert thumbnails.to_displayable(data) == b""


def test_every_ffmpeg_command_in_the_app_carries_the_options() -> None:
    options = decoding.input_options()
    assert options[:2] == ["-protocol_whitelist", "file"] and options[2] == "-format_whitelist"
    assert decoding.input_options(pipe=True)[1] == "file,pipe"
    video = Path("/w/clip.mp4")
    for command in (
        stills._command(video, Path("/o.png"), 1.0),
        stills._preview_command(video, Path("/o.png"), "gif"),
        thumbnails._command(
            MediaItem(path=video, kind=Kind.VIDEO, size=1, mtime=0), video, Path("/o.png")
        ),
    ):
        before_input = command[: command.index("-i")]
        assert "-protocol_whitelist" in before_input and "-format_whitelist" in before_input


def test_every_ffmpeg_command_in_the_app_writes_the_name_it_is_given(tmp_path: Path) -> None:
    """f6 N4: image2's muxer reads a ``%`` in an output name as a pattern, as its demuxer
    did an input's (R42-10): ``pct%dir/t.png`` became ``pct1ir/t.png``. Each command the
    app builds now writes the name it is given, ``%`` and all; a downloaded preview's goes
    to ``pipe:1``, a name with none."""
    folder = tmp_path / "pct%dir"
    folder.mkdir()
    picture = _made(tmp_path / "red.png", "red")
    video = make_video(tmp_path / "clip.mp4")
    item = MediaItem(path=picture, kind=Kind.STILL, size=1, mtime=0)
    with decoding.opened(picture) as opened:
        commands = {
            "thumb%d.png": thumbnails._command(item, opened, folder / "thumb%d.png"),
            "still%d.png": stills._command(video, folder / "still%d.png", 0.0),
            "preview%d.png": stills._preview_command(
                picture, folder / "preview%d.png", "png_pipe"
            ),
        }
        for name, command in commands.items():
            assert _frames(command, tmp_path) == 0, name
            assert (folder / name).is_file(), name
    assert sorted(path.name for path in folder.iterdir()) == sorted(commands)
    assert not (tmp_path / "pct1ir").exists()


# -- S3-3: the gate is inherited by everything below an item folder ----------------------------


@pytest.mark.parametrize(
    "project",
    [
        {"type": "web", "title": "W", "file": "index.html", "preview": "preview.gif"},
        {"type": "application", "title": "A", "file": "run.exe"},
        {"type": "scene", "title": "S", "file": "scene.json"},
        "{ not json",
    ],
)
def test_the_subfolders_of_a_copied_in_item_list_nothing(tmp_path: Path, project: object) -> None:
    root = tmp_path / "library"
    folder = _folder(root, "item", project, ("index.html", "preview.gif"))
    for nested in ("assets/loop.mp4", "assets/deep/bg.gif", "img/bg.png", "a/b/c/d.mp4"):
        (folder / nested).parent.mkdir(parents=True, exist_ok=True)
        (folder / nested).write_bytes(b"x")
    assert _listed(root) == set()


def test_a_copied_in_video_item_lists_only_its_declared_file_and_nothing_below(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    folder = _folder(
        root,
        "v",
        {"type": "video", "title": "V", "file": "clip.mp4", "preview": "preview.gif"},
        ("clip.mp4", "preview.gif", "other.mp4"),
    )
    for nested in ("media/clip.mp4", "media/other.mp4", "bonus/b.mp4", "media/cover.png"):
        (folder / nested).parent.mkdir(parents=True, exist_ok=True)
        (folder / nested).write_bytes(b"x")
    assert _listed(root) == {"v/clip.mp4"}


def test_a_project_json_above_the_library_root_makes_nothing_workshop(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_text(json.dumps({"type": "web"}), encoding="utf-8")
    root = tmp_path / "library"
    _folder(root, "plain", None, ("a.mp4", "b.png"))
    assert _listed(root) == {"plain/a.mp4", "plain/b.png"}
    assert workshop.item_folder_of(root / "plain", (root,)) is None


def test_the_content_tree_is_matched_case_insensitively(tmp_path: Path) -> None:
    path = tmp_path / "x" / "SteamApps" / "Workshop" / "CONTENT" / "431960" / "5" / "sub" / "a.gif"
    path.parent.mkdir(parents=True)
    assert workshop.in_content_tree(path)
    assert workshop.folder_gate(path.parent) == frozenset()
    assert workshop.is_workshop_content(path, labelled=False)
    assert _listed(tmp_path / "x") == set()


def test_a_sidecar_and_a_sibling_below_an_item_folder_are_not_trusted(tmp_path: Path) -> None:
    from wall_in_one.library import pairing

    root = tmp_path / "library"
    folder = _folder(root, "web", {"type": "web", "file": "index.html"}, ("index.html",))
    private = root / "private.png"
    private.write_bytes(b"x")
    for where in (folder, folder / "assets" / "deep"):
        where.mkdir(parents=True, exist_ok=True)
        video = where / "loop.mp4"
        video.write_bytes(b"x")
        (where / "loop-still.png").write_bytes(b"x")
        video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
            json.dumps({"still_path": str(private)}), encoding="utf-8"
        )
        assert pairing.find_still(video, (root,), labelled_workshop=False) is None, where
        assert pairing.read_sidecar(video, (root,)) is None, where


# -- S3-6: stills this app made are never loose wallpapers ---------------------------------------


def test_a_still_0_4_1_drew_from_a_steam_file_inside_a_root_is_not_a_loose_wallpaper(
    tmp_path: Path,
) -> None:
    from wall_in_one.library import pairing

    root = tmp_path / "library"
    item = root / "Games/steam/steamapps/workshop/content/431960/111"
    _folder(
        root,
        "Games/steam/steamapps/workshop/content/431960/111",
        {"type": "web", "file": "index.html", "preview": "preview.gif"},
        ("preview.gif",),
    )
    automatic = pairing.still_directory(root)
    automatic.mkdir(parents=True)
    drawn = automatic / f"{pairing.automatic_still_stem(item / 'preview.gif')}.png"
    drawn.write_bytes(b"\x89PNG")
    mine = automatic / "my-own-picture.png"  # a name this app does not make
    mine.write_bytes(b"\x89PNG")
    (root / "kept.png").write_bytes(b"\x89PNG")

    library = scan.scan([root], records={})

    paths = {i.path for i in library.items}
    assert drawn not in paths, "a still made from Steam's file is not a wallpaper"
    assert mine in paths and root / "kept.png" in paths


def test_every_name_this_app_makes_is_recognised_and_no_other() -> None:
    from wall_in_one.library import pairing

    base = Path("/lib/Wall-in-One/Automatic Stills")
    for name in ("video-0123456789abcdef01234567.png", "123.png", "123-capture.png"):
        assert pairing.is_generated_still(base / name), name
    for name in ("photo.png", "123.jpg", "video-xyz.png", "123-other.png"):
        assert not pairing.is_generated_still(base / name), name
    assert not pairing.is_generated_still(Path("/lib/Other/123.png"))


# -- S3-7: a folder left out for this is said once ---------------------------------------------


def test_a_gated_folder_is_said_once_and_not_when_the_workshop_scan_lists_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    _folder(root, "mine", {"name": "site"}, ("a.png", "b.mp4"))
    library = scan.scan([root], records={})
    said = [u for u in library.unseen if u.path == root / "mine"]
    assert len(said) == 1 and said[0].kind.value == "not a wallpaper"
    assert library.skipped == (), "looked at, not couldn't-look: it blocks nothing"

    steam = tmp_path / "x"
    content = steam / "steamapps/workshop/content/431960"
    _folder(
        content,
        "9",
        {"type": "video", "title": "V", "file": "clip.mp4"},
        ("clip.mp4", "preview.gif"),
    )
    off = scan.scan([content], records={})
    assert [u for u in off.unseen if u.path == content / "9"], "scanning off: said"
    on = scan.scan([content], records={}, include_workshop=True, workshop_roots=(steam,))
    assert not [u for u in on.unseen if u.path == content / "9"], "the Workshop scan lists it"


# -- review-f4 F4-6, U4, F4-11/U8: every project.json up the chain, by name, once per scan -------


def test_a_video_project_nested_in_a_web_item_lists_nothing(tmp_path: Path) -> None:
    """F4-6: the nearest project.json decided, so the author chose what was listed."""
    root = tmp_path / "library"
    web = _folder(root, "web1", {"type": "web", "file": "index.html"}, ("index.html",))
    _folder(web, "assets", {"type": "video", "file": "loop.mp4"}, ("loop.mp4",))
    assert _listed(root) == set()
    assert workshop.folder_gate(web / "assets", (root,)) == frozenset()


def test_an_explicit_root_inside_an_item_folder_lists_its_own_files(tmp_path: Path) -> None:
    """review f5 N4: the gate answers for each root, not once for the folder."""
    root = tmp_path / "library"
    web = _folder(root, "web1", {"type": "web", "file": "index.html"}, ("index.html",))
    assets = _folder(web, "assets", None, ("clip.mp4",))
    assert _listed(root) == set()
    both = scan.scan([root, assets], records={})
    assert {item.path.name for item in both.items} == {"clip.mp4"}
    first = scan.scan([assets, root], records={})
    assert {item.path.name for item in first.items} == {"clip.mp4"}


def test_steams_content_tree_lists_nothing_under_any_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    item = _folder(
        root,
        "steam/steamapps/workshop/content/431960/9",
        {"type": "video", "file": "clip.mp4"},
        ("clip.mp4",),
    )
    assets = _folder(item, "assets", None, ("extra.mp4",))
    for roots in ([root, item], [root, assets], [item, assets]):
        assert scan.scan(roots, records={}).items == (), roots


def _selected(video: Path, still: Path) -> None:
    selector = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    selector.write_text(json.dumps({"still_path": str(still)}), encoding="utf-8")


@pytest.mark.parametrize("order", ["outer-first", "inner-first", "inner-only"])
def test_an_explicit_root_inside_an_item_folder_pairs_what_it_lists(
    tmp_path: Path, order: str
) -> None:
    """R42-12: pairing asks the nearest root, as the walk that listed the video did.

    The first root holding it, with the outer one first, went up to the item's
    ``project.json`` and ignored the selector beside the video it had just listed.
    """
    outer = tmp_path / "library"
    item = _folder(outer, "web1", {"type": "web", "file": "index.html"}, ("index.html",))
    inner = _folder(item, "my-videos", None, ("clip.mp4", "chosen.png"))
    _selected(inner / "clip.mp4", inner / "chosen.png")
    roots = {"outer-first": [outer, inner], "inner-first": [inner, outer], "inner-only": [inner]}

    found = scan.scan(roots[order], records={})
    clip = next(entry for entry in found.items if entry.path == inner / "clip.mp4")

    assert clip.provider == "local" and clip.paired_still == inner / "chosen.png"
    assert not pairing.selector_is_foreign(clip.path, roots[order])


@pytest.mark.parametrize("order", ["outer-first", "inner-first"])
def test_an_item_folders_own_video_stays_untrusted_beside_a_nested_root(
    tmp_path: Path, order: str
) -> None:
    """The video an item declares is listed, and what is beside it is still the author's."""
    outer = tmp_path / "library"
    item = _folder(outer, "video1", {"type": "video", "file": "clip.mp4"}, ("clip.mp4",))
    (outer / "chosen.png").write_bytes(b"x")
    _selected(item / "clip.mp4", outer / "chosen.png")
    (item / "clip-still.png").write_bytes(b"x")
    inner = _folder(item, "my-videos", None, ("own.mp4",))
    roots = [outer, inner] if order == "outer-first" else [inner, outer]

    found = scan.scan(roots, records={})
    clip = next(entry for entry in found.items if entry.path == item / "clip.mp4")

    assert clip.paired_still is None
    assert pairing.read_sidecar(clip.path, roots) is None
    assert pairing.selector_is_foreign(clip.path, roots)


@pytest.mark.parametrize("order", ["outer-first", "inner-first", "inner-only"])
def test_steams_content_tree_stays_untrusted_under_any_root_order(
    tmp_path: Path, order: str
) -> None:
    """A root inside a Workshop item in Steam's tree lists nothing, nor pairs anything."""
    outer = tmp_path / "steam/steamapps/workshop/content/431960"
    item = _folder(outer, "9", {"type": "web", "file": "index.html"}, ("index.html",))
    inner = _folder(item, "my-videos", None, ("clip.mp4", "chosen.png"))
    _selected(inner / "clip.mp4", inner / "chosen.png")
    roots = {"outer-first": [outer, inner], "inner-first": [inner, outer], "inner-only": [inner]}

    assert scan.scan(roots[order], records={}).items == ()
    assert pairing.read_sidecar(inner / "clip.mp4", roots[order]) is None
    assert pairing.selector_is_foreign(inner / "clip.mp4", roots[order])


def test_a_video_project_nested_in_a_video_item_lists_only_the_outer_ones_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    outer = _folder(root, "v1", {"type": "video", "file": "clip.mp4"}, ("clip.mp4",))
    _folder(outer, "more", {"type": "video", "file": "b.mp4"}, ("b.mp4",))
    assert _listed(root) == {"v1/clip.mp4"}


@pytest.mark.parametrize("nested", [False, True], ids=["item", "web-around-it"])
def test_a_root_reached_through_a_linked_folder_lists_a_video_items_file(
    tmp_path: Path, nested: bool
) -> None:
    """U4: the gate held the resolved path; the walk met its own spelling of it."""
    real = tmp_path / "real" / "walls"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    root = tmp_path / "link" / "walls"
    holder = root
    if nested:
        holder = _folder(root, "web1", {"type": "web", "file": "index.html"}, ("index.html",))
    _folder(holder, "item", {"type": "video", "file": "movie.mp4"}, ("movie.mp4", "other.mp4"))
    expected = set() if nested else {"item/movie.mp4"}
    assert _listed(root) == expected
    assert workshop.folder_gate(holder / "item", (root,)) == frozenset(
        {"movie.mp4"} if not nested else ()
    )


def test_one_scan_resolves_each_path_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F4-11, U8: the place tests ran for every video, two or three times, from scratch."""
    root = tmp_path / "library"
    folders = [root / f"a{a}" / f"b{b}" for a in range(3) for b in range(3)]
    for folder in folders:
        folder.mkdir(parents=True)
        for index in range(10):
            (folder / f"v{index}.mp4").write_bytes(b"x")
    real_resolve = Path.resolve
    calls: list[Path] = []

    def counting(self: Path, strict: bool = False) -> Path:
        calls.append(self)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting)
    library = scan.scan([root], records={})
    assert len(library.items) == 90
    # Each video, folder and the root once (100), not a few times per video for each of
    # the walk's and the still readers' place tests (567 without the scan's memo).
    assert len(calls) == len(set(calls)), "a path resolved twice in one scan"
    assert len(calls) <= 90 + len(folders) + 1, len(calls)


def test_nothing_learned_in_one_scan_outlives_it(tmp_path: Path) -> None:
    root = tmp_path / "library"
    folder = _folder(root, "mine", None, ("a.mp4",))
    assert _listed(root) == {"mine/a.mp4"}
    (folder / "project.json").write_text(json.dumps({"type": "web"}), encoding="utf-8")
    assert _listed(root) == set(), "the next scan sees the project.json"
    assert workshop._SCAN_MEMO.get() is None


def test_a_selector_beside_a_hand_copied_video_item_is_not_the_apps_to_delete(
    tmp_path: Path,
) -> None:
    """review-f4 F4-9: the Workshop branch of `selector_is_foreign`, which no test told apart.

    The same app-shaped selector, naming a still inside the root, is the app's own beside a
    video of yours, and the author's beside an item's video: a removal leaves that one.
    """
    root = tmp_path / "library"
    still = root / "Wall-in-One" / "Automatic Stills" / "chosen.png"
    still.parent.mkdir(parents=True)
    still.write_bytes(b"png")
    for name, project in (("mine", None), ("item", {"type": "video", "file": "clip.mp4"})):
        folder = _folder(root, name, project, ("clip.mp4",))
        video = folder / "clip.mp4"
        video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
            json.dumps({"still_path": str(still)}), encoding="utf-8"
        )
        assert pairing.selector_is_foreign(video, (root,)) is (project is not None), name
