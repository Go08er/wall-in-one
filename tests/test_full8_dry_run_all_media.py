"""AE-2 (L-3): a dry run that leaves All media unplayable says so in a removal's words."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wall_in_one import config, dry_run
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
        assert "can’t be looked into" in text
        assert "cannot compile" not in text
        # The compiler's own sentence is still what `compile_error` holds.
        assert "cannot compile" in verdict.compile_error
    finally:
        session.shutdown()
