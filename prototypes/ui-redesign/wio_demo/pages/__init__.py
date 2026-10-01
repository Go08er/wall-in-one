"""Page contract: the app's own (``wall_in_one.ui.next.page``).

The shell owns one header bar and swaps each page's widgets in. A page module
exposes ``create(state) -> Page``. Keep page modules independent: they talk to
each other only through ``state`` (``state.navigate("playlist:frog-day")``,
``state.toast(...)``, ``state.emit_changed(topic)``). The Library page and its
inspector are the app's (``wall_in_one.ui.next.library``).
"""

from __future__ import annotations

from wall_in_one.ui.next.page import Page, Placeholder

__all__ = ["Page", "Placeholder"]
