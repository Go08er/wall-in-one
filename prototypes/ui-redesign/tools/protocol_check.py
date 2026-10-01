"""The demo's AppState implements the app's Protocols: checked by mypy, never run.

``tools/typecheck.sh`` runs mypy on this file with the prototype's own modules
followed silently (they are not typed to the app's standard); only these
assignments are judged. The app's adapter is checked the same way in
``tests/test_next_state.py``.
"""

from __future__ import annotations

from wall_in_one.ui.next.state import AppState, LibraryEditing, PlaybackControls
from wall_in_one.ui.next.thumbs import ThumbnailProvider
from wio_demo.state import AppState as DemoAppState
from wio_demo.thumbs import Loader


def _state(state: DemoAppState) -> AppState:
    return state


def _controls(state: DemoAppState) -> PlaybackControls:
    return state


def _editing(state: DemoAppState) -> LibraryEditing:
    return state


def _pictures(loader: Loader) -> ThumbnailProvider:
    return loader
