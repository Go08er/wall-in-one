"""The one-time Tidy up card, in the real classic application on the golden profile.

It must offer the tidy-up, write nothing while it is shown, and write
``ui.toml`` only when the user dismisses it; once dismissed it never plans
again. (Kept apart from ``test_ui_tidy`` because these run whole applications.)
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.golden import harness, sandbox  # noqa: E402
from tests.golden.harness import Allowance, Change  # noqa: E402
from tests.golden.test_idle import first_start_writes  # noqa: E402
from tests.test_ui_next_slice import _cache_touch  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    Step,
    application_lanes,
    run_application,
    settled,
)
from wall_in_one import paths, tidy, ui_prefs  # noqa: E402
from wall_in_one import thumbnails as thumbnail_cache  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402
from wall_in_one.wallpaper import outputs, scenes  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sandbox.Golden]:
    runtime_dir = Path(tempfile.mkdtemp(prefix="wio-run-"))
    try:
        yield sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _quiet_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nix check's machine: no ffmpeg, engine or niri, so idle writes no stills."""

    def no_ffmpeg(item: MediaItem, **_keywords: object) -> Path:
        raise thumbnail_cache.ThumbnailError(f"no processes in the golden sandbox: {item.path}")

    monkeypatch.setattr(thumbnail_cache, "generate", no_ffmpeg)
    monkeypatch.setattr(scenes, "is_available", lambda: False)
    monkeypatch.setattr(outputs, "is_available", lambda: False)


def _whitelist(golden: sandbox.Golden) -> list[Allowance]:
    return [
        *first_start_writes(golden.profile, None),
        Allowance(
            ".cache/wall-in-one/thumbnails/*",
            frozenset({"rewritten"}),
            "the GUI's thumbnail cache re-stamps an entry it served (bytes unchanged)",
            _cache_touch,
        ),
    ]


def _dismissal(change: Change) -> None:
    assert change.after is not None and change.after.content is not None
    assert b"tidy_offer_dismissed = true" in change.after.content


def test_the_card_offers_tidy_up_writes_nothing_and_a_dismissal_writes_only_ui_toml(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_machine(monkeypatch)
    home = golden.profile.home
    before = harness.snapshot(home)
    application = Application()
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        card = window._tidy_card
        yield (
            "the first scan and the card's check",
            lambda: settled(application) and card.checked and not application.tidy_lane.busy,
        )
        assert card.get_reveal_child(), "the golden profile has leftovers to offer"
        body = card.body.get_text()
        assert "archive old leftovers" in body and "fix the palette template name" in body
        assert "thumbnail" not in body
        harness.check_changes(harness.diff(before, harness.snapshot(home)), _whitelist(golden))
        assert not paths.ui_prefs_path().exists(), "showing the card writes nothing"

        card.review.emit("clicked")
        section = window._settings_page.tidy
        yield (
            "the dismissal saved and the section planned",
            lambda: (
                paths.ui_prefs_path().exists()
                and not application.tidy_lane.busy
                and section.row(tidy.LEFTOVERS) is not None
            ),
        )
        assert not card.get_reveal_child()
        assert window._stack.get_visible_child_name() == "settings"
        leftovers = section.row(tidy.LEFTOVERS)
        assert leftovers is not None and leftovers.expander.get_expanded()
        assert ui_prefs.load().prefs.tidy_offer_dismissed
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    harness.check_changes(
        harness.diff(before, harness.snapshot(home)),
        [
            *_whitelist(golden),
            Allowance(
                ".config/wall-in-one/ui.toml",
                frozenset({"created"}),
                "the user dismissed the Tidy up card",
                _dismissal,
            ),
            Allowance(
                ".config/wall-in-one/.ui.toml.mutation.lock",
                frozenset({"created"}),
                "ui.toml's writer lock, taken for that one save",
            ),
        ],
    )


def test_a_dismissed_card_stays_hidden_and_never_plans(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_machine(monkeypatch)
    ui_prefs.update({"tidy_offer_dismissed": True})
    planned: list[object] = []
    real_plan = tidy.plan

    def counted(**keywords: Any) -> tidy.Plan:
        planned.append(keywords)
        return real_plan(**keywords)

    monkeypatch.setattr(tidy, "plan", counted)
    home = golden.profile.home
    before = harness.snapshot(home)
    application = Application()
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        card = window._tidy_card
        yield (
            "the first scan and the card's check",
            lambda: settled(application) and card.checked and not application.tidy_lane.busy,
        )
        opened = time.monotonic()
        yield "a moment idle", lambda: time.monotonic() > opened + 0.5
        assert not card.get_reveal_child() and card.plan is None and planned == []
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    harness.check_changes(harness.diff(before, harness.snapshot(home)), _whitelist(golden))
