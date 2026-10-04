"""What the new interface's Schedule and Displays pages need from the app.

The schedule's rules, and each display's assignment and defaults.
`AppState.scheduling` returns a `Scheduling`, or None while the pages are not
ported or the app can't offer them; they then show their "not in the new
interface yet" placeholders. The real one lives beside the adapter in
``real_schedule.py``; the prototype's demo state is the other.
"""

from __future__ import annotations

from typing import Protocol


class Scheduling(Protocol):
    """The Schedule and Displays pages' view of the app. Its members arrive with them."""
