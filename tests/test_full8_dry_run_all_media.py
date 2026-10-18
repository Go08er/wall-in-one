"""AE-2 (L-3): a dry run that leaves All media unplayable says so in a removal's words."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wall_in_one import config, dry_run, runtime_config
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.library.model import Kind, Library, MediaItem, Unseen, UnseenKind
from wall_in_one.session import Session


def test_deleting_the_last_wallpaper_beside_an_unreadable_folder_is_worded_as_a_removal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    locked = root / "locked"
    picture = MediaItem(root / "picture.png", Kind.STILL, 1, 1)
    settings = config.Settings(roots=(root,), scan_workshop=False)
    session = Session(
        settings,
        favourite_store=favourites.Store(),
        pairing_store=pairings.Store(),
        playlist_store=playlists.Store(),
        schedule_store=schedules.Store(),
        display_store=displays.Store(),
        removal_store=removals.Store(),
        owns_playback=False,
    )
    try:
        library = Library(
            roots=(root,),
            items=(picture,),
            unseen=(Unseen(locked, UnseenKind.UNREADABLE, f"{locked}: Permission denied"),),
        )
        session.adopt_library(library)

        verdict = dry_run.removal(
            picture,
            settings=settings,
            library=library,
            roots=(root,),
            adopted_authority=None,
            favourite_store=session.favourites,
            pairing_store=session.pairings,
            playlist_store=session.playlists,
            uses=dry_run.Uses.of(session),
            scanner=lambda _roots: replace(library, items=()),
        )

        assert len(verdict.refusals) == 1
        text = verdict.refusals[0]
        assert text.startswith(f"All media would have nothing to play while {locked} ")
        assert "can\u2019t be looked into" in text
        assert "choose a default playlist" in text
        assert "remove the reference" not in text
        assert "cannot compile" not in text
        # The compiler's own sentence is still what `compile_error` holds.
        assert "cannot compile" in verdict.compile_error
    finally:
        session.shutdown()


def test_deleting_the_last_compiling_star_names_the_rule_that_names_all_media(
    tmp_path: Path,
) -> None:
    """A default is chosen already: the way out is the rule, not 'choose a default'."""
    root = tmp_path / "library"
    star = MediaItem(root / "star.png", Kind.STILL, 1, 1)
    video = MediaItem(root / "video.mp4", Kind.VIDEO, 1, 1)
    other = MediaItem(root / "other.png", Kind.STILL, 1, 1)
    named = playlists.Playlist(
        id="good", name="Good", entries=(playlists.Entry(id="o", source=str(other.path)),)
    )
    settings = config.Settings(
        roots=(root,), scan_workshop=False, active_playlist="good", cycle_favourites_only=True
    )
    session = Session(
        settings,
        favourite_store=favourites.Store(favourites.Favourites((star.path, video.path))),
        pairing_store=pairings.Store(),
        playlist_store=playlists.Store({"good": named}),
        schedule_store=schedules.Store(
            (schedules.Rule(id="night", playlist=runtime_config.FALLBACK_PLAYLIST_ID),)
        ),
        display_store=displays.Store(),
        removal_store=removals.Store(),
        owns_playback=False,
    )
    try:
        library = Library(
            roots=(root,),
            items=(star, video, other),
            unseen=(Unseen(video.path, UnseenKind.UNREADABLE, f"{video.path}: its still"),),
        )
        session.adopt_library(library)

        verdict = dry_run.removal(
            star,
            settings=settings,
            library=library,
            roots=(root,),
            adopted_authority=None,
            favourite_store=session.favourites,
            pairing_store=session.pairings,
            playlist_store=session.playlists,
            uses=dry_run.Uses.of(session),
            scanner=lambda _roots: library,
        )

        assert len(verdict.refusals) == 1
        text = verdict.refusals[0]
        assert text.startswith(f"All media would have nothing to play while {video.path} ")
        assert "remove the reference in schedule rule 'night'" in text
        assert "choose a default playlist" not in text
    finally:
        session.shutdown()
