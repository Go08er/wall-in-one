"""The real `LibraryFolders`: the Library's "Add a folder", and F5's rescan.

Adding a folder is the Settings page's own path (`SettingsAccess.add_folder`,
`real_settings.RealSettings`), so there is one way a folder joins the
library, with classic Settings' safeguards:

* a folder already in the library is not added twice;
* only a folder the person chose is ever added. A folder Wall-in-One detects
  (Noctalia's wallpaper directory) is a suggestion, not consent (DECISIONS.md):
  nothing here adds one, and the first folder added -- the one downloads and
  generated stills go to -- is always one the person picked;
* the saved ``roots`` change on the authoring lane, the application rescans
  only once that save succeeds, and "Scanning" is said only then; a refused
  or failed save (settings read-only, say) is reported and changes nothing.

The outcome is reported by that path, so `add_library_folder` returns no
Undo; a folder is taken out again in Settings.
"""

from __future__ import annotations

from collections.abc import Callable

from wall_in_one.ui.next.settings_state import SettingsAccess
from wall_in_one.ui.next.state import Undo


class RealFolders:
    """`LibraryFolders` over the Settings part's library folders."""

    def __init__(self, settings: SettingsAccess, rescan: Callable[[], None]) -> None:
        self._settings = settings
        self._rescan = rescan

    def add_folder_blocked(self) -> str:
        return self._settings.settings_blocked()

    def add_library_folder(self, path: str) -> Undo | None:
        """Add ``path`` the way Settings does; the outcome is reported when saved."""
        self._settings.add_folder(path)
        return None

    def rescan(self) -> None:
        self._rescan()
