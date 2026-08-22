"""Pure authoring decisions for mirrored and independent displays.

No compositor calls live here.  The GUI and the future schema compiler can
hand these helpers one already-discovered output snapshot and get the same
answer without making a second device query or silently changing a saved
docking preference.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThemeSource:
    """Saved and effective owners of Noctalia's one shell-wide palette."""

    configured: str
    effective: str
    configured_attached: bool

    @property
    def is_fallback(self) -> bool:
        return bool(self.configured) and not self.configured_attached


def resolve_theme_source(
    configured: str,
    connected: Iterable[str],
    *,
    primary: str = "",
) -> ThemeSource:
    """Choose the live colour source without rewriting ``configured``.

    A present saved connector always wins.  When it is detached, use the
    compositor's primary connector when that is known and live, otherwise the
    first live connector in the caller's stable output order.  The returned
    ``configured`` value remains the detached name so reconnecting a dock
    restores the user's explicit choice.
    """
    saved = configured.strip()
    ordered = tuple(dict.fromkeys(name.strip() for name in connected if name.strip()))
    if saved and saved in ordered:
        return ThemeSource(saved, saved, True)
    preferred = primary.strip()
    effective = preferred if preferred in ordered else ordered[0] if ordered else ""
    return ThemeSource(saved, effective, not saved)
