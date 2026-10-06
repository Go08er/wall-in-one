"""A saved settings batch that can't be adopted settles itself, and only itself (Z-1).

Settings edits are coalesced into batches by generation. A batch whose write
landed but whose adoption on GTK failed went through the lane's failure
route, into `_fail_settings_batch`, which is for a batch that did not save:
it cleared every pending field and callback, so a newer edit accepted
during the save was dropped (its tail ran empty) and told the older batch's
"Saved, but it couldn't be applied"; the saved batch's own callbacks, taken
before adoption, were never settled. A saved batch now settles its own
generation exactly once, whatever adopting it does, and a newer generation
stays pending and saves as its own batch.

The adoption fault is the window's `apply_settings` failing once; the
writes are real `config.update` calls, held after publication so the newer
edit arrives during the save.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
    toolkit,
)
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library import playlists  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


class _AdoptionFailsOnce(FakeWindow):
    """The window can't take the first settings it is given."""

    def __init__(self) -> None:
        super().__init__()
        self.applied: list[float] = []

    def apply_settings(self, settings: config.Settings) -> None:
        self.applied.append(settings.opacity)
        if len(self.applied) == 1:
            raise RuntimeError("the window could not take it")


def _held_after_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], threading.Event, threading.Event]:
    """Every real settings write, the first held right after it published."""
    real = config.update
    writes: list[dict[str, Any]] = []
    published = threading.Event()
    release = threading.Event()

    def update(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        saved = real(changes, *arguments, **keywords)
        writes.append(dict(changes))
        if len(writes) == 1:
            published.set()
            assert release.wait(5)
        return saved

    monkeypatch.setattr(config, "update", update)
    return writes, published, release


def _lane_idle(application: Application) -> bool:
    return (
        not application._settings_authoring_running
        and not application._authoring_active
        and not application._authoring_queue
    )


def test_a_newer_edit_survives_an_older_saved_batch_that_could_not_be_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex's cell: opacity 0.7 saved but not adopted, 0.4 accepted during its save."""
    application = _application(tmp_path, monkeypatch)
    window = _AdoptionFailsOnce()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    writes, published, release = _held_after_publishing(monkeypatch)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            opacity=0.7,
            on_success=lambda saved: outcomes.append(("0.7 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.7 failed", message)),
        )
        assert published.wait(5), "the first write published"
        assert application.update_settings_async(
            opacity=0.4,
            on_success=lambda saved: outcomes.append(("0.4 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.4 failed", message)),
        )
        release.set()
        spin_until(lambda: _lane_idle(application), timeout=10)

        # Before: one write, 0.7 kept, and the newer edit told the older one's
        # "Saved, but it couldn't be applied" though it never saved.
        assert [write["opacity"] for write in writes] == [0.7, 0.4], "two real writes"
        assert config.load_strict().opacity == 0.4
        assert application.settings.opacity == 0.4
        assert outcomes == [("0.7 saved", 0.7), ("0.4 saved", 0.4)]
        unapplied = [r for r in window.reports if "saved, but they couldn't be applied" in r]
        assert len(unapplied) == 1 and "the window could not take it" in unapplied[0]
        assert not any("nothing changed" in r for r in window.reports), window.reports
    finally:
        release.set()
        _close(application)


def test_a_saved_batch_whose_finish_raises_later_still_keeps_the_newer_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a saved batch reaching the lane's saved-but-not-applied route is not
    the batch's pre-commit failure, which would drop the newer accepted edit."""
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    writes, published, release = _held_after_publishing(monkeypatch)
    real_finish = Application._finish_settings_batch
    finished: list[object] = []

    def finish_then_fail(self: Application, result: Any) -> None:
        real_finish(self, result)
        finished.append(result)
        if len(finished) == 1:
            raise RuntimeError("after the batch settled")

    monkeypatch.setattr(Application, "_finish_settings_batch", finish_then_fail)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            opacity=0.7, on_success=lambda saved: outcomes.append(("0.7 saved", saved.opacity))
        )
        assert published.wait(5)
        assert application.update_settings_async(
            opacity=0.4,
            on_success=lambda saved: outcomes.append(("0.4 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.4 failed", message)),
        )
        release.set()
        spin_until(lambda: _lane_idle(application), timeout=10)

        assert [write["opacity"] for write in writes] == [0.7, 0.4], "the tail kept and saved"
        assert config.load_strict().opacity == 0.4
        assert outcomes == [("0.7 saved", 0.7), ("0.4 saved", 0.4)]
        assert not any("nothing changed" in r for r in window.reports), window.reports
    finally:
        release.set()
        _close(application)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def test_the_default_playlist_bookkeeping_settles_against_the_generation_that_saved(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default A, then B during A's save, A's adoption failing once: B saves, nothing sticks."""
    application = Application(ui="next")
    lanes: list[object] = []
    writes, published, release = _held_after_publishing(monkeypatch)
    real_apply = NextWindow.apply_settings
    applied: list[str] = []

    def apply_once_fails(window: NextWindow, settings: config.Settings) -> None:
        applied.append(settings.active_playlist)
        if len(applied) == 1:
            raise RuntimeError("the window could not take it")
        real_apply(window, settings)

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        store = application.session.playlists
        a = store.create("Alpha", entries=[playlists.Entry("a1", wallpaper(tmp_path, "dawn.png"))])
        b = store.create("Beta", entries=[playlists.Entry("b1", wallpaper(tmp_path, "dusk.jpg"))])
        application.playlists_changed()
        yield "the playlists to be published", lambda: settled(application)
        editing = window.state.playlist_editing
        assert editing is not None
        monkeypatch.setattr(NextWindow, "apply_settings", apply_once_fails)
        editing.make_default(a.id)
        yield "A to publish", published.is_set
        editing.make_default(b.id)
        release.set()
        yield "both to settle", lambda: settled(application)

        # Before: A stayed saved, the adapter still showed B as the default,
        # and A stayed "asked" for good.
        assert [write["active_playlist"] for write in writes] == [a.id, b.id]
        assert config.load_strict().active_playlist == b.id
        assert application.settings.active_playlist == b.id
        adapter: Any = editing  # its bookkeeping, as the review found it
        assert adapter._defaults == {} and adapter._default_asked == {}
        detail_a, detail_b = editing.detail(a.id), editing.detail(b.id)
        assert detail_a is not None and not detail_a.is_default
        assert detail_b is not None and detail_b.is_default
        assert not any("nothing changed" in m for m in said), said
        yield from finish(application, window, lanes)

    try:
        run(application, scenario(), lanes)
    finally:
        release.set()


def test_a_newer_edit_survives_an_older_batch_that_did_not_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-commit side: a refused write puts back its own generation, not a newer one.

    `_prepare_settings_batch` always said that changes arriving after its
    snapshot "stay pending and start their own batch"; the failure path
    cleared them anyway, telling the newer edit the older one's refusal.
    """
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    real = config.update
    writes: list[dict[str, Any]] = []
    started = threading.Event()
    release = threading.Event()

    def update(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        if not writes:
            writes.append({})
            started.set()
            assert release.wait(5)
            raise config.ConfigError("cannot save settings.toml: No space left on device")
        writes.append(dict(changes))
        return real(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", update)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            opacity=0.7,
            on_success=lambda saved: outcomes.append(("0.7 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.7 failed", message)),
        )
        assert started.wait(5)
        assert application.update_settings_async(
            shuffle=True,
            on_success=lambda saved: outcomes.append(("shuffle saved", saved.shuffle)),
            on_error=lambda message: outcomes.append(("shuffle failed", message)),
        )
        release.set()
        spin_until(lambda: _lane_idle(application), timeout=10)

        # Before: the shuffle edit was dropped and told "No space left on device".
        assert outcomes == [
            ("0.7 failed", "ConfigError: cannot save settings.toml: No space left on device"),
            ("shuffle saved", True),
        ]
        assert writes[1:] == [{"shuffle": True}], "the newer edit saved as its own batch"
        saved = config.load_strict()
        assert saved.shuffle and saved.opacity != 0.7
        assert application.settings.shuffle and application.settings.opacity == saved.opacity
    finally:
        release.set()
        _close(application)


def test_a_saved_batch_whose_finish_fails_before_settling_is_settled_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane's route never wedges a batch: one whose finish raised before taking its
    generation is settled from what settings.toml holds, and the newer tail still saves."""
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    writes, published, release = _held_after_publishing(monkeypatch)
    real_finish = Application._finish_settings_batch
    calls: list[object] = []

    def fails_first(self: Application, result: Any) -> None:
        calls.append(result)
        if len(calls) == 1:
            raise RuntimeError("before the batch was settled")
        real_finish(self, result)

    monkeypatch.setattr(Application, "_finish_settings_batch", fails_first)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            opacity=0.7,
            on_success=lambda saved: outcomes.append(("0.7 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.7 failed", message)),
        )
        assert published.wait(5)
        assert application.update_settings_async(
            opacity=0.4,
            on_success=lambda saved: outcomes.append(("0.4 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.4 failed", message)),
        )
        release.set()
        spin_until(lambda: _lane_idle(application), timeout=10)

        assert [write["opacity"] for write in writes] == [0.7, 0.4]
        assert config.load_strict().opacity == 0.4
        assert outcomes == [("0.7 saved", 0.7), ("0.4 saved", 0.4)]
        assert any("Saved, but it couldn't be applied" in r for r in window.reports)
        assert not any("nothing changed" in r for r in window.reports), window.reports
    finally:
        release.set()
        _close(application)


@pytest.mark.parametrize("service", [False, True], ids=["window", "service"])
def test_a_source_change_that_could_not_be_adopted_settles_each_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: bool
) -> None:
    """New library folders save, then adopting them fails: their callers hear "saved",
    once; the opacity edit accepted during that save saves as its own batch.

    In the window the failure comes before its scan of the new folders began,
    so that scan is started now. In the compatibility service it is the scan
    itself (inside `Session.update_settings`), which is not repeated: the
    library stays what it was until a scan succeeds (Z-2). Before, the saved
    batch's callbacks were never called, and the newer edit was dropped.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    first, second = tmp_path / "first", tmp_path / "second"
    for root in (first, second):
        root.mkdir()
        (root / f"{root.name}.png").write_bytes(b"placeholder image bytes")
    config.save(config.Settings(roots=(first,), scan_workshop=False))
    application = Application(service=service, ui="classic")
    application._authoring_migration_ready = True
    window = FakeWindow() if service else _AdoptionFailsOnce()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    monkeypatch.setattr(application, "_sources_check", lambda: lambda _before, _after: None)
    scans: list[str] = []
    if service:
        real_refresh = Session.refresh

        def refresh(session: Session, *arguments: Any, **keywords: Any) -> Any:
            scans.append("scan")
            if len(scans) == 1:
                raise RuntimeError("injected: the rescan failed")
            return real_refresh(session, *arguments, **keywords)

        monkeypatch.setattr(Session, "refresh", refresh)
        expected_failure = "injected: the rescan failed"
    else:
        real_refresh_library = application.refresh_library

        def refresh_library(**keywords: Any) -> None:
            scans.append("scan")
            real_refresh_library(**keywords)

        monkeypatch.setattr(application, "refresh_library", refresh_library)
        expected_failure = "the window could not take it"
    writes, published, release = _held_after_publishing(monkeypatch)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            roots=(first, second),
            on_success=lambda saved: outcomes.append(("folders saved", saved.roots)),
            on_error=lambda message: outcomes.append(("folders failed", message)),
        )
        assert published.wait(5)
        assert application.update_settings_async(
            opacity=0.4,
            on_success=lambda saved: outcomes.append(("0.4 saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("0.4 failed", message)),
        )
        release.set()
        spin_until(lambda: _lane_idle(application), timeout=10)

        assert [sorted(write) for write in writes] == [["roots"], ["opacity"]]
        saved = config.load_strict()
        assert saved.roots == (first, second) and saved.opacity == 0.4
        assert outcomes == [("folders saved", (first, second)), ("0.4 saved", 0.4)]
        unapplied = [r for r in window.reports if "saved, but they couldn't be applied" in r]
        assert len(unapplied) == 1 and expected_failure in unapplied[0], window.reports
        assert scans == ["scan"], "one scan of the new folders: started, or the failed one"
        assert not any("nothing changed" in r for r in window.reports), window.reports
    finally:
        release.set()
        application._shutdown_library_scan_jobs(wait=True)
        _close(application)
