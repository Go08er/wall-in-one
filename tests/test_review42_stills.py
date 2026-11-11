"""Codex review 0.4.2: Still-policy diagnostics (R42-5, R42-6); no real worker, renderer or desktop.
Run only through tools/isolated.sh. The parent owns execution; these assertions
express intended behavior, so the two candidate failures are findings.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_workshop_still_sources import _Tree
from wall_in_one import config
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.ui import stills as ui_stills
from wall_in_one.ui.next import real_state
from wall_in_one.wallpaper import outputs, scenes


def _web_item(path: Path) -> MediaItem:
    return MediaItem(
        path=path,
        kind=Kind.SCENE,
        size=1,
        mtime=0,
        scene="300",
        workshop_type="web",
        web_page=True,
        web_files=True,
    )


@pytest.mark.parametrize("disabled", ["previews", "web"])
def test_a_policy_skip_does_not_permanently_memoize_a_queued_web_still(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disabled: str
) -> None:
    state = {"previews": True, "web": True}
    made = ui_stills.StillMaker(
        displays=lambda: (),
        web_allowed=lambda: state["web"],
        previews=lambda: state["previews"],
    )
    queued: list[tuple[Callable[..., None], tuple[Any, ...]]] = []

    def hold(work: Callable[..., None], *arguments: Any, **_keywords: Any) -> None:
        queued.append((work, arguments))

    monkeypatch.setattr(made, "_submit", hold)
    monkeypatch.setattr(outputs, "discover", lambda *_args: ())
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_args: (64, 64))
    item = _web_item(tmp_path / "web")
    try:
        made.request((item,), tmp_path, lambda _count: None)
        assert len(queued) == 1
        state[disabled] = False
        work, arguments = queued[0]
        work(*arguments)  # real _run and ensure; policy refuses before touching content
        state[disabled] = True
        made.request((item,), tmp_path, lambda _count: None)
        assert len(queued) == 2, (
            f"{disabled} was off when the queued item was reached; enabling it "
            "again must allow an automatic still attempt"
        )
    finally:
        made.shutdown()


def test_preview_off_before_admission_does_not_memoize_the_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = [False]
    made = ui_stills.StillMaker(
        displays=lambda: (), web_allowed=lambda: True, previews=lambda: state[0]
    )
    queued: list[object] = []
    monkeypatch.setattr(made, "_submit", lambda *args, **_kwargs: queued.append(args))
    item = _web_item(tmp_path / "web")
    try:
        made.request((item,), tmp_path, lambda _count: None)
        assert queued == []
        state[0] = True
        made.request((item,), tmp_path, lambda _count: None)
        assert len(queued) == 1
    finally:
        made.shutdown()


@pytest.mark.parametrize("choice", ["old-preview", "manual", "none"])
def test_only_an_accepted_manual_still_is_shown_as_an_active_manual_choice(
    tmp_path: Path, choice: str
) -> None:
    tree = _Tree(tmp_path)
    original = tree.scan()["300"]
    identity = pairings.Identity.of(original)
    old = tree.automatic / "300.png"
    chosen = old if choice == "old-preview" else tree.mine
    records = (
        {}
        if choice == "none"
        else {identity.key: pairings.Pairing(identity=identity, still=chosen, customized=True)}
    )
    settings = config.Settings(
        roots=(tree.root,), allow_web_wallpapers=True, web_wallpaper_previews=False
    )
    _settings, session = tree.session(pairings.Store(records), settings=settings)
    try:
        web = next(item for item in session.library.items if item.scene == "300")
        accepted = tree.mine if choice == "manual" else None
        assert old.is_file() and web.paired_still == accepted
        assert session.pairings.resolve_accepted(web, session.library).still == accepted
        indexed = frozenset(
            candidate.path
            for candidate in (*session.library.items, *session.library.still_inventory)
            if candidate.kind is Kind.STILL
        )
        shown = SimpleNamespace(
            _session=session,
            _editing=None,
            _refused_reason=lambda _item: "",
        )
        view = real_state.RealAppState._view_of(shown, web, lambda: indexed)  # type: ignore[arg-type]
        if choice == "manual":
            assert "Chosen by hand" in view.still_note, view.still_note
        else:
            assert "Chosen by hand" not in view.still_note, view.still_note
            assert "choose one by hand" in view.still_note, view.still_note
    finally:
        session.shutdown()
