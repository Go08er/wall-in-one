from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from wall_in_one import config
from wall_in_one.library import pairing, scan
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership, classify
from wall_in_one.library.playlist import Playlist


def _touch(path: Path, body: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _item(path: Path, kind: Kind = Kind.STILL, **extra: object) -> MediaItem:
    return MediaItem(path=path, kind=kind, size=1, mtime=0, **extra)  # type: ignore[arg-type]


# -- classification ------------------------------------------------------


def test_classify_covers_stills_videos_and_neither() -> None:
    assert classify(Path("a.png")) is Kind.STILL
    assert classify(Path("a.JPEG")) is Kind.STILL
    assert classify(Path("a.mp4")) is Kind.VIDEO
    assert classify(Path("a.txt")) is None
    assert classify(Path("a")) is None


def test_gif_is_a_video() -> None:
    """A still gif shows one frame, which is not what setting a gif means."""
    assert classify(Path("loop.gif")) is Kind.VIDEO


# -- playback ------------------------------------------------------------


def test_paused_video_falls_back_to_its_still(tmp_path: Path) -> None:
    still = tmp_path / "clip-still.png"
    video = _item(tmp_path / "clip.mp4", Kind.VIDEO).with_still(still)

    assert video.playback_path(dynamics_enabled=True) == tmp_path / "clip.mp4"
    assert video.playback_path(dynamics_enabled=False) == still


def test_paused_video_without_a_still_is_unplayable(tmp_path: Path) -> None:
    video = _item(tmp_path / "clip.mp4", Kind.VIDEO)
    assert video.playback_path(dynamics_enabled=False) is None

    library = Library(roots=(tmp_path,), items=(video,))
    assert library.playable(dynamics_enabled=True) == (video,)
    assert library.playable(dynamics_enabled=False) == ()


def test_only_managed_items_are_deletable(tmp_path: Path) -> None:
    assert not _item(tmp_path / "a.png").deletable
    assert _item(tmp_path / "b.png", ownership=Ownership.MANAGED).deletable


# -- pairing -------------------------------------------------------------


def test_sibling_still_suffix_pairs(tmp_path: Path) -> None:
    video = _touch(tmp_path / "snowy-village.mp4")
    still = _touch(tmp_path / "snowy-village-still.png")
    assert pairing.find_still(video) == still


def test_plain_sibling_name_pairs(tmp_path: Path) -> None:
    video = _touch(tmp_path / "cabin.mp4")
    still = _touch(tmp_path / "cabin.png")
    assert pairing.find_still(video) == still


def test_gif_is_never_chosen_as_a_still(tmp_path: Path) -> None:
    video = _touch(tmp_path / "loop.mp4")
    _touch(tmp_path / "loop.gif")
    assert pairing.find_still(video) is None


def test_sidecar_beats_the_naming_convention(tmp_path: Path) -> None:
    video = _touch(tmp_path / "clip.mp4")
    _touch(tmp_path / "clip-still.png")
    chosen = _touch(tmp_path / "elsewhere" / "chosen.png")
    (tmp_path / ("clip.mp4" + pairing.SIDECAR_SUFFIX)).write_text(
        json.dumps({"still_path": str(chosen)}), encoding="utf-8"
    )
    assert pairing.find_still(video) == chosen


def test_sidecar_naming_a_missing_file_is_ignored(tmp_path: Path) -> None:
    video = _touch(tmp_path / "clip.mp4")
    convention = _touch(tmp_path / "clip-still.png")
    (tmp_path / ("clip.mp4" + pairing.SIDECAR_SUFFIX)).write_text(
        json.dumps({"still_path": str(tmp_path / "gone.png")}), encoding="utf-8"
    )
    assert pairing.find_still(video) == convention


def test_automatic_stills_directory_is_searched(tmp_path: Path) -> None:
    video = _touch(tmp_path / "videos" / "clip.mp4")
    generated = _touch(tmp_path / "Wall-in-One" / pairing.AUTOMATIC_STILLS_DIRECTORY / "clip.png")
    assert pairing.find_still(video, roots=[tmp_path]) == generated


def test_apply_drops_stills_that_only_represent_a_video(tmp_path: Path) -> None:
    video = _item(tmp_path / "clip.mp4", Kind.VIDEO)
    paired = _item(tmp_path / "clip-still.png")
    standalone = _item(tmp_path / "china-town-still.png")

    result = pairing.apply([video, paired, standalone], resolver={video.path: paired.path})

    assert [item.path for item in result] == [video.path, standalone.path]
    assert result[0].paired_still == paired.path


def test_apply_leaves_an_unpaired_video_alone(tmp_path: Path) -> None:
    video = _item(tmp_path / "clip.mp4", Kind.VIDEO)
    (paired,) = pairing.apply([video], resolver={})
    assert paired.paired_still is None


# -- scanning ------------------------------------------------------------


def test_scan_finds_media_and_skips_sidecars(tmp_path: Path) -> None:
    _touch(tmp_path / "a.png")
    _touch(tmp_path / "b.mp4")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / ("b.mp4" + pairing.SIDECAR_SUFFIX), b"{}")

    library = scan.scan([tmp_path])

    assert sorted(item.path.name for item in library.items) == ["a.png", "b.mp4"]


def test_scan_ignores_hidden_directories(tmp_path: Path) -> None:
    _touch(tmp_path / "visible.png")
    _touch(tmp_path / ".hidden" / "secret.png")

    library = scan.scan([tmp_path])

    assert [item.path.name for item in library.items] == ["visible.png"]


def test_scan_reports_a_missing_root_instead_of_failing(tmp_path: Path) -> None:
    library = scan.scan([tmp_path / "nope"])
    assert library.items == ()
    assert any("not a directory" in note for note in library.skipped)


def test_managed_needs_both_a_directory_marker_and_a_file_sidecar(tmp_path: Path) -> None:
    managed = tmp_path / "Wall-in-One" / "MotionBGS"
    _touch(
        managed / ".wall-in-one-motionbgs-managed.json",
        json.dumps({"provider": "MotionBGS"}).encode(),
    )
    _touch(managed / "downloaded.mp4")
    _touch(managed / "downloaded.mp4.motionbgs.json", b"{}")
    # Dropped in by hand: the directory is ours, this file is not.
    _touch(managed / "mine.mp4")

    library = scan.scan([tmp_path])
    by_name = {item.path.name: item for item in library.items}

    assert by_name["downloaded.mp4"].ownership is Ownership.MANAGED
    assert by_name["downloaded.mp4"].provider == "MotionBGS"
    assert by_name["mine.mp4"].ownership is Ownership.USER
    assert not by_name["mine.mp4"].deletable


def test_scan_pairs_what_it_finds(tmp_path: Path) -> None:
    _touch(tmp_path / "clip.mp4")
    _touch(tmp_path / "clip-still.png")

    library = scan.scan([tmp_path])

    assert len(library.items) == 1
    assert library.items[0].kind is Kind.VIDEO
    assert library.items[0].paired_still == tmp_path / "clip-still.png"


def test_scan_does_not_follow_directory_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _touch(outside / "elsewhere.png")
    root = tmp_path / "root"
    root.mkdir()
    _touch(root / "inside.png")
    (root / "link").symlink_to(outside, target_is_directory=True)

    library = scan.scan([root])

    assert [item.path.name for item in library.items] == ["inside.png"]


def test_scan_honours_the_depth_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "MAX_DEPTH", 1)
    _touch(tmp_path / "top.png")
    _touch(tmp_path / "one" / "deep.png")
    _touch(tmp_path / "one" / "two" / "deeper.png")

    library = scan.scan([tmp_path])

    assert sorted(item.path.name for item in library.items) == ["deep.png", "top.png"]
    assert any("deeper than" in note for note in library.skipped)


def test_wallpaper_directory_read_from_noctalia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    wallpapers = tmp_path / "pictures"
    wallpapers.mkdir()
    settings = tmp_path / "state" / "noctalia" / "settings.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text(f'[wallpaper]\ndirectory = "{wallpapers}"\n', encoding="utf-8")

    assert scan.wallpaper_directory_from_noctalia() == wallpapers
    assert scan.default_roots() == (wallpapers,)


def test_missing_noctalia_settings_is_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert scan.wallpaper_directory_from_noctalia() is None


# -- playlist ------------------------------------------------------------


def _playlist(count: int, **kwargs: object) -> Playlist:
    items = [_item(Path(f"/w/{index}.png")) for index in range(count)]
    return Playlist(items, **kwargs)  # type: ignore[arg-type]


def test_empty_playlist_answers_none() -> None:
    empty = Playlist()
    assert empty.current() is None
    assert empty.next() is None
    assert empty.previous() is None
    assert empty.random() is None


def test_sequential_next_wraps() -> None:
    playlist = _playlist(3)
    current = playlist.current()
    assert current is not None and current.name == "0"
    assert playlist.next().name == "1"  # type: ignore[union-attr]
    assert playlist.next().name == "2"  # type: ignore[union-attr]
    assert playlist.next().name == "0"  # type: ignore[union-attr]


def test_previous_wraps_backwards() -> None:
    playlist = _playlist(3)
    assert playlist.previous().name == "2"  # type: ignore[union-attr]


def test_shuffle_visits_everything_before_repeating() -> None:
    playlist = _playlist(8, shuffle=True, rng=random.Random(7))
    seen = [playlist.current()]
    for _ in range(7):
        seen.append(playlist.next())
    names = sorted(item.name for item in seen if item is not None)
    assert names == sorted(str(index) for index in range(8))


def test_shuffle_does_not_repeat_across_the_seam() -> None:
    """A reshuffle must not put the wallpaper you are looking at back up."""
    for seed in range(30):
        playlist = _playlist(4, shuffle=True, rng=random.Random(seed))
        for _ in range(3):
            playlist.next()
        last = playlist.current()
        assert playlist.next() != last


def test_previous_retraces_the_shuffled_path() -> None:
    playlist = _playlist(6, shuffle=True, rng=random.Random(3))
    first = playlist.current()
    second = playlist.next()
    third = playlist.next()
    assert playlist.previous() == second
    assert playlist.previous() == first
    assert third != first


def test_toggling_shuffle_keeps_your_place() -> None:
    playlist = _playlist(6)
    playlist.next()
    playlist.next()
    current = playlist.current()

    playlist.set_shuffle(True)
    assert playlist.current() == current
    playlist.set_shuffle(False)
    assert playlist.current() == current


def test_random_never_picks_where_it_already_is() -> None:
    playlist = _playlist(5, rng=random.Random(1))
    for _ in range(40):
        before = playlist.current()
        assert playlist.random() != before


def test_random_with_one_item_stays_put() -> None:
    playlist = _playlist(1)
    assert playlist.random() == playlist.current()


def test_select_moves_the_cursor() -> None:
    playlist = _playlist(4)
    assert playlist.select(Path("/w/2.png"))
    assert playlist.current().name == "2"  # type: ignore[union-attr]
    assert not playlist.select(Path("/w/nope.png"))


def test_set_items_keeps_the_current_wallpaper_if_it_survives() -> None:
    playlist = _playlist(4)
    playlist.next()
    playlist.next()
    current = playlist.current()
    assert current is not None

    kept = [item for item in playlist.items if item.name in {"1", "2", "3"}]
    playlist.set_items(kept)
    assert playlist.current() == current


def test_set_items_falls_back_to_the_start_when_it_does_not() -> None:
    playlist = _playlist(4)
    playlist.next()
    playlist.next()

    remaining = [item for item in playlist.items if item.name in {"0", "1"}]
    playlist.set_items(remaining)
    current = playlist.current()
    assert current is not None and current.name == "0"


# -- configured roots -----------------------------------------------------
#
# The library used to be whatever Noctalia's single `wallpaper.directory` said.
# Anyone whose wallpapers sat in two places saw half of them, with nothing
# anywhere to say so.


def test_roots_default_to_empty_meaning_ask_noctalia() -> None:
    assert config.Settings().roots == ()


def test_roots_survive_a_toml_round_trip(tmp_path: Path) -> None:
    settings = config.Settings(roots=(tmp_path / "one", tmp_path / "two"))
    written = tmp_path / "settings.toml"
    config.save(settings, written)
    assert config.load(written).roots == (tmp_path / "one", tmp_path / "two")


def test_an_empty_root_list_survives_the_round_trip(tmp_path: Path) -> None:
    """It is written out rather than omitted: a setting nobody can see is a
    setting nobody knows they have."""
    written = tmp_path / "settings.toml"
    config.save(config.Settings(), written)
    assert "roots = []" in written.read_text(encoding="utf-8")
    assert config.load(written).roots == ()


def test_a_root_with_an_awkward_name_survives(tmp_path: Path) -> None:
    """A quote or a backslash in a directory name must not produce bad TOML."""
    awkward = tmp_path / 'quote"and\\slash'
    written = tmp_path / "settings.toml"
    config.save(config.Settings(roots=(awkward,)), written)
    assert config.load(written).roots == (awkward,)


def test_a_duplicate_root_is_dropped(tmp_path: Path) -> None:
    """Scanning a directory twice would put everything in it into the
    rotation twice."""
    settings = config.Settings(roots=(tmp_path, tmp_path)).validated()
    assert settings.roots == (tmp_path,)


def test_a_root_written_with_a_tilde_is_the_same_root_expanded() -> None:
    """`~/Pictures` and `/home/you/Pictures` are the duplicate a person adds."""
    settings = config.Settings(roots=(Path("~/Pictures"), Path.home() / "Pictures")).validated()
    assert settings.roots == (Path.home() / "Pictures",)


def test_root_order_is_kept_because_the_first_one_receives_downloads(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "b", tmp_path / "a"
    assert config.Settings(roots=(first, second)).validated().roots == (first, second)


def test_a_roots_entry_that_is_not_a_string_costs_only_that_entry() -> None:
    raw = {"roots": ["/one", 7, "", "/two"]}
    assert config.Settings.from_mapping(raw).roots == (Path("/one"), Path("/two"))


def test_roots_that_are_not_a_list_are_ignored_rather_than_fatal() -> None:
    assert config.Settings.from_mapping({"roots": "/one"}).roots == ()
