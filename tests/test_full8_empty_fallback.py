"""AE-2: an empty effective All media must not turn 'couldn't look' into 'nothing there'."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, Library, MediaItem, Unseen, UnseenKind
from wall_in_one.session import Session

ALL_MEDIA = runtime_config.FALLBACK_PLAYLIST_ID


def _session(settings: config.Settings) -> Session:
    return Session(
        settings,
        favourite_store=favourites.Store(),
        pairing_store=pairings.Store(),
        playlist_store=playlists.Store(),
        schedule_store=schedules.Store(),
        display_store=displays.Store(),
        removal_store=removals.Store(),
        owns_playback=False,
    )


def _unseen(path: Path, kind: UnseenKind) -> Unseen:
    why = "scan entry limit" if kind is UnseenKind.LIMIT else "Permission denied"
    return Unseen(path, kind, f"{path}: {why}")


@pytest.mark.parametrize("default", ("", ALL_MEDIA, "All media", "missing-default"))
@pytest.mark.parametrize("kind", (UnseenKind.UNREADABLE, UnseenKind.LIMIT))
def test_an_empty_effective_fallback_refuses_an_incomplete_scan_and_keeps_runtime(
    tmp_path: Path, default: str, kind: UnseenKind
) -> None:
    """Failed at a225e74: the empty fallback silently replaced a good document."""
    root = tmp_path / "library"
    settings = config.Settings(roots=(root,), scan_workshop=False, active_playlist=default)
    session = _session(settings)
    target = tmp_path / "runtime.toml"
    try:
        picture = MediaItem(root / "picture.png", Kind.STILL, 1, 1)
        session.adopt_library(Library(roots=(root,), items=(picture,)))
        runtime_config.publish_changes(settings, session, target)
        previous = target.read_bytes()
        place = _unseen(root, kind)
        session.adopt_library(Library(roots=(root,), items=(), unseen=(place,)))

        with pytest.raises(runtime_config.RuntimeConfigError) as refused:
            runtime_config.publish_changes(settings, session, target)

        assert str(root) in str(refused.value)
        assert "left untouched" in str(refused.value)
        assert target.read_bytes() == previous
    finally:
        session.shutdown()


@pytest.mark.parametrize("default", ("", ALL_MEDIA, "All media"))
@pytest.mark.parametrize("absent", (False, True), ids=("clean empty", "root not usable"))
def test_an_empty_fallback_is_allowed_when_nothing_could_not_be_looked_at(
    tmp_path: Path, default: str, absent: bool
) -> None:
    root = tmp_path / "library"
    settings = config.Settings(roots=(root,), scan_workshop=False, active_playlist=default)
    session = _session(settings)
    try:
        unseen = (_unseen(root, UnseenKind.ROOT_NOT_USABLE),) if absent else ()
        session.adopt_library(Library(roots=(root,), items=(), unseen=unseen))

        built = runtime_config.compiled(settings, session)

        document = tomllib.loads(built.document)
        assert document["default_playlist"] == ALL_MEDIA
        assert document["playlists"] == [{"id": ALL_MEDIA, "name": "All media"}]
        assert built.left_out == ()
    finally:
        session.shutdown()


def _unused_empty_fallback(
    tmp_path: Path,
    *,
    rules: tuple[schedules.Rule, ...] = (),
    assignments: dict[str, str] | None = None,
    independent: bool = False,
) -> tuple[config.Settings, Session]:
    """The starred video has no still; a good named default holds an unstarred picture.

    Thus the built-in favourites-only fallback is empty without making the
    named default empty. Its missing still is unknown, not known absent.
    """
    root = tmp_path / "library"
    picture = MediaItem(root / "picture.png", Kind.STILL, 1, 1)
    video = MediaItem(root / "video.mp4", Kind.VIDEO, 1, 1)
    named = playlists.Playlist(
        id="good-default",
        name="Good default",
        entries=(playlists.Entry(id="picture", source=str(picture.path)),),
    )
    settings = config.Settings(
        roots=(root,),
        scan_workshop=False,
        active_playlist=named.id,
        cycle_favourites_only=True,
        display_mode=(
            config.DISPLAY_MODE_INDEPENDENT if independent else config.DISPLAY_MODE_MIRRORED
        ),
        theme_source_connector="DP-1" if independent else "",
    )
    session = Session(
        settings,
        favourite_store=favourites.Store(favourites.Favourites((video.path,))),
        pairing_store=pairings.Store(),
        playlist_store=playlists.Store({named.id: named}),
        schedule_store=schedules.Store(rules),
        display_store=displays.Store(assignments),
        removal_store=removals.Store(),
        owns_playback=False,
    )
    session.adopt_library(
        Library(
            roots=(root,),
            items=(picture, video),
            unseen=(_unseen(video.path, UnseenKind.UNREADABLE),),
        )
    )
    return settings, session


@pytest.mark.parametrize(
    "dormant", ("unused", "disabled rule", "mirrored rule", "mirrored display")
)
def test_an_unused_empty_fallback_does_not_block_a_good_named_default(
    tmp_path: Path, dormant: str
) -> None:
    rules: tuple[schedules.Rule, ...] = ()
    if dormant in ("disabled rule", "mirrored rule"):
        rules = (
            schedules.Rule(
                id="dormant",
                playlist=ALL_MEDIA,
                enabled=dormant != "disabled rule",
                connector="DP-1" if dormant == "mirrored rule" else "",
            ),
        )
    settings, session = _unused_empty_fallback(
        tmp_path,
        rules=rules,
        assignments={"DP-1": ALL_MEDIA} if dormant == "mirrored display" else None,
    )
    try:
        built = runtime_config.compiled(settings, session)

        assert tomllib.loads(built.document)["default_playlist"] == "good-default"
        assert built.left_out == ()
    finally:
        session.shutdown()


@pytest.mark.parametrize("use", ("rule", "connector rule", "display"))
@pytest.mark.parametrize("reference", (ALL_MEDIA, "All media"))
def test_an_enabled_fallback_reference_refuses_when_its_empty_inventory_is_unseen(
    tmp_path: Path, use: str, reference: str
) -> None:
    """Failed at a225e74: explicit fallback references bypassed the unseen test too."""
    rules = (
        (
            schedules.Rule(
                id="uses-fallback",
                playlist=reference,
                connector="DP-1" if use == "connector rule" else "",
            ),
        )
        if use != "display"
        else ()
    )
    settings, session = _unused_empty_fallback(
        tmp_path,
        rules=rules,
        assignments={"DP-1": reference} if use == "display" else None,
        independent=use != "rule",
    )
    target = tmp_path / "runtime.toml"
    try:
        # The same inventory, completely seen, can be published as before.
        incomplete = session.library
        session.adopt_library(replace(incomplete, unseen=()))
        runtime_config.publish_changes(settings, session, target)
        previous = target.read_bytes()
        session.adopt_library(incomplete)

        with pytest.raises(runtime_config.RuntimeConfigError) as refused:
            runtime_config.publish_changes(settings, session, target)

        assert str(tmp_path / "library" / "video.mp4") in str(refused.value)
        assert target.read_bytes() == previous
    finally:
        session.shutdown()


def test_a_nonempty_fallback_does_not_refuse_a_partly_unseen_inventory(tmp_path: Path) -> None:
    settings, session = _unused_empty_fallback(tmp_path)
    settings = replace(settings, active_playlist="", cycle_favourites_only=False)
    try:
        built = runtime_config.compiled(settings, session)

        document = tomllib.loads(built.document)
        assert document["default_playlist"] == ALL_MEDIA
        assert len(document["playlists"][0]["entries"]) == 1
        assert built.left_out == ()
    finally:
        session.shutdown()


def test_a_favourites_subset_does_not_refuse_over_an_unrelated_unseen_folder(
    tmp_path: Path,
) -> None:
    settings, session = _unused_empty_fallback(tmp_path)
    settings = replace(settings, active_playlist=ALL_MEDIA)
    try:
        session.adopt_library(
            replace(
                session.library,
                unseen=(_unseen(tmp_path / "library" / "unrelated", UnseenKind.UNREADABLE),),
            )
        )

        built = runtime_config.compiled(settings, session)

        assert tomllib.loads(built.document)["default_playlist"] == ALL_MEDIA
        assert built.left_out == ()
    finally:
        session.shutdown()
