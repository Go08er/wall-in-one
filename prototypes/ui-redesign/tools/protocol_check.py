"""The demo's AppState implements the app's Protocols: checked by mypy, never run.

``tools/typecheck.sh`` runs mypy on this file with the prototype's own modules
followed silently (they are not typed to the app's standard); only these
assignments are judged. The app's adapter is checked the same way in
``tests/test_next_state.py``.
"""

from __future__ import annotations

from wio_demo.state import AppState as DemoAppState
from wio_demo.state import DemoStoreItem
from wio_demo.thumbs import Loader

from wall_in_one.ui.next.playlists_state import PlaylistEditing
from wall_in_one.ui.next.schedule_state import Scheduling
from wall_in_one.ui.next.settings_state import PaletteManager, SettingsAccess
from wall_in_one.ui.next.state import AppState, LibraryEditing, LibraryFolders, PlaybackControls
from wall_in_one.ui.next.store_state import StoreAccess, StoreItemView
from wall_in_one.ui.next.thumbs import ThumbnailProvider


def _state(state: DemoAppState) -> AppState:
    return state


def _controls(state: DemoAppState) -> PlaybackControls:
    return state


def _editing(state: DemoAppState) -> LibraryEditing:
    return state


def _folders(state: DemoAppState) -> LibraryFolders:
    return state


def _settings(state: DemoAppState) -> SettingsAccess:
    return state


def _palettes(state: DemoAppState) -> PaletteManager:
    return state.palette_manager


def _playlists(state: DemoAppState) -> PlaylistEditing:
    return state


def _scheduling(state: DemoAppState) -> Scheduling:
    return state


def _pictures(loader: Loader) -> ThumbnailProvider:
    return loader


def _store(state: DemoAppState) -> StoreAccess:
    return state


def _store_item(item: DemoStoreItem) -> StoreItemView:
    return item
