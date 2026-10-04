"""The display mode and the desktop colors' display: one rule for every page that saves them.

Settings → Displays (`real_settings.RealSettings`) and the Displays page
(`real_schedule.RealScheduling`) both change ``display_mode`` and
``theme_source_connector`` in ``settings.toml``. Both work out the edit
here, then save it through the application's one validated
``update_settings_async``, so the two pages can't disagree about what is
refused or what is saved, and both show what is durable through the
``settings`` topic.

As classic Settings does: independent displays need a display whose
wallpaper Noctalia's one palette follows. The saved choice is kept (even
detached: the runtime then follows a connected stand-in until it is back);
without one, the first *attached* display is chosen, and with none attached
the change is refused (`DECISIONS.md`, "Mirrored is the default").

GTK-free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from wall_in_one import config

#: Why independent displays were refused (classic Settings' words).
NEEDS_AN_ATTACHED_DISPLAY: Final = (
    "Independent display control needs an attached display for Colours follow. Connect a "
    "display and try again."
)


def settings_edit(
    requested: config.Settings, changes: Mapping[str, Any], attached: Sequence[str]
) -> dict[str, Any] | str:
    """``changes`` less what is saved or being saved already, or why they are refused.

    ``requested`` is what is saved with every queued edit applied
    (`Application.requested_settings`); ``attached`` the connectors connected
    now, GTK's monitors first. Switching to independent displays without a
    saved colors' display adds the first attached one.
    """
    wanted = {key: value for key, value in changes.items() if getattr(requested, key) != value}
    if wanted.get("display_mode") == config.DISPLAY_MODE_INDEPENDENT and not (
        wanted.get("theme_source_connector") or requested.theme_source_connector
    ):
        if not attached:
            return NEEDS_AN_ATTACHED_DISPLAY
        wanted["theme_source_connector"] = attached[0]
    return wanted
