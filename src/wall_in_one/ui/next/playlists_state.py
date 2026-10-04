"""What the new interface's Playlists page needs from the app.

Creating, renaming, reordering and removing playlists and their entries.
`AppState.playlist_editing` returns a `PlaylistEditing`, or None while the page
is not ported or the app can't offer it; the page then shows its "not in the
new interface yet" placeholder. The real one lives beside the adapter in
``real_playlists.py``; the prototype's demo state is the other.
"""

from __future__ import annotations

from typing import Protocol


class PlaylistEditing(Protocol):
    """The Playlists page's view of the app. Its members arrive with the page."""
