"""What the new interface's Store page needs from the app.

Browsing the providers and downloading into the library. `AppState.store_access`
returns a `StoreAccess`, or None while the page is not ported or the app can't
offer it; the page then shows its "not in the new interface yet" placeholder.
The real one lives beside the adapter in ``real_store.py``; the prototype's
demo state is the other.
"""

from __future__ import annotations

from typing import Protocol


class StoreAccess(Protocol):
    """The Store page's view of the app. Its members arrive with the page."""
