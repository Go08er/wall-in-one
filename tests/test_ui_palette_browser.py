"""The palette browser's origin tables, which nothing else checks.

Only the tables are tested here, not the widgets: building a dialog needs a
display, and the failure this guards against does not. `_rebuild_browse`
iterates every `Origin` and indexes both dicts, so a member added to the enum
without a matching entry is a `KeyError` the moment the browser opens -- which
is exactly what happened when `LEGACY` arrived.
"""

from __future__ import annotations

from wall_in_one.theme.palettes import Origin
from wall_in_one.ui.palette_browser import _ORIGIN_DESCRIPTIONS, _ORIGIN_EMPTY


def test_every_origin_has_a_description() -> None:
    assert set(_ORIGIN_DESCRIPTIONS) == set(Origin)


def test_every_origin_has_something_to_say_when_it_is_empty() -> None:
    assert set(_ORIGIN_EMPTY) == set(Origin)


def test_the_unapplicable_origin_is_named_in_its_own_description() -> None:
    """A greyed-out Apply button needs the group text to explain itself."""
    for origin in Origin:
        if not origin.is_applicable:
            assert "uplicate" in _ORIGIN_DESCRIPTIONS[origin]
