"""What the new interface's Settings page needs from the app.

The app's settings and its palettes. `AppState.settings_access` returns a
`SettingsAccess`, or None while the page is not ported or the app can't offer
it; the page then keeps today's placeholder, which already holds the Interface
and GPU acceleration rows. The real one lives beside the adapter in
``real_settings.py``; the prototype's demo state is the other.
"""

from __future__ import annotations

from typing import Protocol


class SettingsAccess(Protocol):
    """The Settings page's view of the app. Its members arrive with the page."""
