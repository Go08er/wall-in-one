"""Automatic-still maintenance on the golden profile when no capture can be made.

Both interfaces hand every scan's items to `ui.stills.StillMaker`, which asks
`library.stills.ensure` for each moving item that has no still or is a scene
with a managed still. `harness.run_idle` never constructs the application, so
the golden idle round trip never runs it; these tests make the same `ensure`
calls without GTK, for every item the maker could be handed.

The golden scene still is a 2x2 PNG. With no display measured it is kept (a
still is never judged against the 2560x1440 capture fallback); with one
measured it is due, being smaller in both directions. Either way no capture
can be made without the engine, so nothing may be written. (An engine that is
there but cannot be started is the one accepted exception, pinned below.)
Before the fix
`capture_scene` created its temporary before asking for the engine, and the
temporary's cleanup left an empty ``entry-XXXXXXXX`` claim directory and a
0-byte ``entry-<32 hex>`` file under ``.wall-in-one-retained``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.golden import harness
from tests.golden.sandbox import Golden
from wall_in_one import config
from wall_in_one.library import stills
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session
from wall_in_one.wallpaper import outputs, scenes

#: The owner's panel, as niri reports it.
DISPLAY = (2560, 1600)


def _scanned(golden: Golden) -> tuple[tuple[MediaItem, ...], Path]:
    """The library the application's first scan adopts, and its write root."""
    del golden
    session = Session(config.load_document().settings)
    try:
        session.adopt_library_refresh(session.prepare_library_refresh().run())
        return session.library.items, session.library.roots[0]
    finally:
        session.shutdown()


def _maintained(items: tuple[MediaItem, ...], root: Path) -> list[MediaItem]:
    """Everything `StillMaker.request` may hand to its worker."""
    return [
        item
        for item in items
        if item.kind.moves
        and (
            item.paired_still is None
            or (
                item.kind is Kind.SCENE
                and item.paired_still == stills.automatic_destination(item, root)
            )
        )
    ]


def _scene(items: tuple[MediaItem, ...]) -> MediaItem:
    (scene,) = [item for item in items if item.kind is Kind.SCENE]
    return scene


@pytest.fixture
def no_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nix check's PATH: no linux-wallpaperengine, no niri to measure a display."""
    monkeypatch.setattr(scenes, "is_available", lambda: False)
    monkeypatch.setattr(outputs, "is_available", lambda: False)


def test_the_golden_scene_still_is_never_judged_against_a_guess(
    golden: Golden, no_renderer: None
) -> None:
    items, root = _scanned(golden)
    scene = _scene(items)
    still = stills.automatic_destination(scene, root)
    assert still is not None and scene.paired_still == still
    assert stills._png_size(still) == (2, 2)
    assert scenes.measured_capture_size() is None, "no display was measured"
    assert not stills.scene_capture_required(scene, root, automatic=True)
    assert not stills.scene_capture_required(scene, root)
    # Only the fixture's 2x2 makes it due once a display is measured.
    assert stills.scene_capture_required(scene, root, size=DISPLAY, automatic=True)


def test_idle_still_maintenance_without_a_renderer_writes_nothing(
    golden: Golden, no_renderer: None
) -> None:
    items, root = _scanned(golden)
    wanted = _maintained(items, root)
    assert _scene(items) in wanted, "the maker has the scene to consider"
    before = harness.snapshot(golden.profile.home)
    made = [stills.ensure(item, root) for item in wanted]
    changes = harness.diff(before, harness.snapshot(golden.profile.home))
    assert made == [item.paired_still for item in wanted], "every still was kept"
    assert not golden.processes
    assert [change.describe() for change in changes] == []


def test_a_due_scene_without_a_renderer_writes_nothing(
    golden: Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A display was measured, so the 2x2 still is due, but there is no engine."""
    monkeypatch.setattr(scenes, "is_available", lambda: False)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_arguments: DISPLAY)
    items, root = _scanned(golden)
    scene = _scene(items)
    before = harness.snapshot(golden.profile.home)
    assert stills.ensure(scene, root) is None
    assert stills.ensure(scene, root, size=DISPLAY) is None
    changes = harness.diff(before, harness.snapshot(golden.profile.home))
    assert not golden.processes
    assert [change.describe() for change in changes] == []


RETAINED = "Pictures/Wallpapers/Wall-in-One/Automatic Stills/.wall-in-one-retained/"


def assert_one_claim_pair(changes: list[harness.Change]) -> None:
    """Exactly what withdrawing one named temporary leaves, by `file_io`'s design:
    an empty ``entry-XXXXXXXX`` claim directory and a 0-byte ``entry-<32 hex>``."""
    assert all(change.kind == "created" and change.after for change in changes), changes
    claims = [c for c in changes if c.after is not None and c.after.kind == "dir"]
    tombstones = [c for c in changes if c.after is not None and c.after.kind == "file"]
    assert len(claims) == 1 and len(tombstones) == 1, [c.describe() for c in changes]
    assert re.fullmatch(re.escape(RETAINED) + r"entry-[a-z0-9_]{8}", claims[0].path)
    assert re.fullmatch(re.escape(RETAINED) + r"entry-[0-9a-f]{32}", tombstones[0].path)
    assert tombstones[0].after is not None and tombstones[0].after.size == 0


def test_a_due_scene_whose_engine_cannot_be_started_leaves_only_its_claim(
    golden: Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepted for now: engine installed and display measured, process refused.

    The engine needs a named ``.png`` path, so the temporary exists before the
    process is started, and withdrawing it leaves one claim pair. Nothing else
    may change: the still is kept.
    """
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_arguments: DISPLAY)
    items, root = _scanned(golden)
    scene = _scene(items)
    started: list[object] = []

    def refused(arguments: object, *_args: object, **_kwargs: object) -> None:
        started.append(arguments)
        raise PermissionError(13, "process start refused")

    monkeypatch.setattr(subprocess, "Popen", refused)
    before = harness.snapshot(golden.profile.home)
    assert stills.ensure(scene, root, size=DISPLAY) is None
    changes = harness.diff(before, harness.snapshot(golden.profile.home))
    assert len(started) == 1, "the engine really was asked"
    assert_one_claim_pair(changes)
