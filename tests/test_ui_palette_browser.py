"""The palette browser's origin tables, which nothing else checks.

Only the tables are tested here, not the widgets: building a dialog needs a
display, and the failure this guards against does not. `_rebuild_browse`
iterates every `Origin` and indexes both dicts, so a member added to the enum
without a matching entry is a `KeyError` the moment the browser opens -- which
is exactly what happened when `LEGACY` arrived.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest

from wall_in_one.theme.palettes import Origin
from wall_in_one.ui.palette_browser import (
    _ORIGIN_DESCRIPTIONS,
    _ORIGIN_EMPTY,
    PaletteBrowserDialog,
)


def test_every_origin_has_a_description() -> None:
    assert set(_ORIGIN_DESCRIPTIONS) == set(Origin)


def test_every_origin_has_something_to_say_when_it_is_empty() -> None:
    assert set(_ORIGIN_EMPTY) == set(Origin)


def test_the_unapplicable_origin_is_named_in_its_own_description() -> None:
    """A greyed-out Apply button needs the group text to explain itself."""
    for origin in Origin:
        if not origin.is_applicable:
            assert "uplicate" in _ORIGIN_DESCRIPTIONS[origin]


def test_noctalia_sync_failure_reports_the_already_saved_app_scheme() -> None:
    saved: list[tuple[str, bool]] = []
    reports: list[str] = []

    def use_preview_scheme(
        scheme: str,
        *,
        sync_noctalia: bool,
        on_complete: Callable[[str], None] | None,
    ) -> None:
        saved.append((scheme, sync_noctalia))
        assert on_complete is not None
        on_complete("shell IPC unavailable")

    application = SimpleNamespace(use_preview_scheme=use_preview_scheme)
    raw_dialog = SimpleNamespace(
        _app=application,
        _sync=SimpleNamespace(get_active=lambda: True),
        _closed=False,
        report=reports.append,
    )
    raw_dialog._on_scheme_synced = lambda scheme, error: PaletteBrowserDialog._on_scheme_synced(
        cast(PaletteBrowserDialog, raw_dialog), scheme, error
    )
    dialog = cast(PaletteBrowserDialog, raw_dialog)
    PaletteBrowserDialog._on_use_scheme(dialog, "vibrant")

    assert saved == [("vibrant", True)]
    assert reports == [
        "App scheme changed to vibrant, but Noctalia was not updated: shell IPC unavailable"
    ]


def test_successful_scheme_sync_reports_once_and_queues_one_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports: list[str] = []
    scheduled: list[Callable[[], bool]] = []

    def timeout_add(_delay: int, callback: Callable[[], bool]) -> int:
        scheduled.append(callback)
        return 1

    def settle() -> bool:
        return False

    monkeypatch.setattr("wall_in_one.ui.palette_browser.GLib.timeout_add", timeout_add)
    dialog = cast(
        PaletteBrowserDialog,
        SimpleNamespace(_closed=False, report=reports.append, _settle=settle),
    )

    PaletteBrowserDialog._on_scheme_synced(dialog, "vibrant", "")

    assert reports == ["scheme vibrant"]
    assert scheduled == [settle]
