"""Codex 0.3.0 round 4, in the real application: what is published after a change.

The dry run decides whether a change is made, and only that. What is
published comes from reality: after a removal commits, a real scan on the
same worker, and nothing new until it lands. The invariant every finding
of round 4 broke, and the one these hold the application to for every
conformance scenario (`test_dry_run.SCENARIOS`, run through the real
application here): every runtime configuration installed after a change
names only files that exist when it is installed; a refused change installs
none. S-1, a capture finishing while the removal is being checked, is
reproduced with the application's own StillMaker batch. Each of Codex's
fails on 7ad0dd6.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_dry_run import SCENARIOS, Harness, _file, _missing  # noqa: E402
from tests.test_ui_codex030_round2 import _lane_idle  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config, dry_run, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import stills, workshop  # noqa: E402
from wall_in_one.session import RemovalResult, Session  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class Installed:
    """Every runtime configuration installed, and what it named that wasn't there then."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.documents: list[tuple[str, list[str]]] = []
        # What the application's lanes install through (`ui.app._runtime_installed`).
        install = runtime_config.publish_changes

        def installed(
            settings: config.Settings, session: Any, path: Path | None = None
        ) -> runtime_config.Published:
            changed = install(settings, session, path)
            if changed:
                target = path if path is not None else paths.runtime_config_path()
                document = target.read_text(encoding="utf-8")
                self.documents.append((document, _missing(document)))
            return changed

        monkeypatch.setattr(runtime_config, "publish_changes", installed)

    def since(self, count: int) -> list[tuple[str, list[str]]]:
        return self.documents[count:]


class AppHarness(Harness):
    """The conformance scenarios' state, held by the real application's session."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.patch = monkeypatch
        self.tmp_path = tmp_path
        self.applications: dict[int, Application] = {}

    def __call__(self, roots: tuple[Path, ...], steam: Path | None = None) -> Session:
        if steam is not None:
            self.patch.setattr(workshop, "steam_roots", lambda *_a, **_k: (steam,))
        application = _application(self.tmp_path, self.patch)
        _attach(application, FakeWindow())
        # Stills are S-1's own test; here a scan landing must not start ffmpeg.
        self.patch.setattr(application, "_make_missing_stills", lambda: None)
        self.patch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        session = application.session
        self.applications[id(session)] = application
        self._save(application, roots, scan_workshop=steam is not None)
        return session

    def rescan(self, session: Session, roots: tuple[Path, ...]) -> None:
        application = self.application(session)
        self._save(application, roots, scan_workshop=application.settings.scan_workshop)

    def application(self, session: Session) -> Application:
        return self.applications[id(session)]

    @staticmethod
    def _save(application: Application, roots: tuple[Path, ...], *, scan_workshop: bool) -> None:
        settings = replace(application.settings, roots=roots, scan_workshop=scan_workshop)
        settings = settings.validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        application.session.update_settings(settings, rescan_library=False)
        application.session.refresh(mutate_removals=False)
        application._accepted_library_sources = (settings.roots, settings.scan_workshop)

    def close(self) -> None:
        for application in self.applications.values():
            _close(application)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AppHarness]:
    made = AppHarness(tmp_path, monkeypatch)
    try:
        yield made
    finally:
        made.close()


def _settled(application: Application) -> bool:
    return (
        _lane_idle(application)
        and not application._runtime_compile_pending
        and application._library_scan_future is None
    )


def _publish(application: Application) -> bytes:
    assert application._publish_runtime_async()
    document = paths.runtime_config_path()
    spin_until(lambda: _settled(application) and document.exists(), 10, what="a publication")
    return document.read_bytes()


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_generation_installed_after_a_change_names_only_files_that_exist(
    name: str, harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-commit invariant, through the application, for every conformance scenario."""
    installed = Installed(monkeypatch)
    scenario = SCENARIOS[name](harness, harness.tmp_path)
    application = harness.application(scenario.session)
    before = _publish(application)
    count = len(installed.documents)
    finished: list[RemovalResult] = []
    errors: list[str] = []

    if isinstance(scenario.change, Path):
        item = application.session.library.find(scenario.change)
        assert item is not None
        application.remove_item_async(item, trash=True, finish=finished.append)
    else:
        application.update_settings_async(
            roots=scenario.change.roots,
            scan_workshop=scenario.change.scan_workshop,
            on_error=errors.append,
        )
    spin_until(lambda: _settled(application), 30, what="the change and what follows it")
    if not scenario.refused:
        # A change of sources is followed by its scan; let it land and publish.
        spin_until(
            lambda: (
                _settled(application)
                and application.session.library.roots == application.settings.roots
            ),
            30,
            what="the scan after the change",
        )

    after = installed.since(count)
    assert [missing for _document, missing in after if missing] == [], (
        "a generation named a file that wasn't there when it was installed"
    )
    if scenario.refused:
        assert finished == []
        assert after == []
        assert paths.runtime_config_path().read_bytes() == before
    elif isinstance(scenario.change, Path):
        assert [result.committed for result in finished] == [True]
        assert not scenario.change.exists()
    else:
        assert errors == []


def test_s1_a_capture_finishing_while_its_removal_is_checked_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex S-1, with the application's own StillMaker batch for A and B.

    A's frame is held until the removal's dry run starts scanning, then let
    go. The removal holds A's lifecycle locks from before its dry run to
    after its commit, so A's capture can't land in between; once A is gone it
    is discarded. What is published comes from the scan after the commit.
    """
    root = tmp_path / "lib" / "R"
    a = _file(root / "a.mp4")
    _file(root / "b.mp4")
    _file(root / "k.png")
    application = _application(tmp_path, monkeypatch)
    _attach(application, FakeWindow())
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.refresh(mutate_removals=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    installed = Installed(monkeypatch)
    capture = stills.destination(a, root)
    a_rendering = threading.Event()
    release_a = threading.Event()
    release_b = threading.Event()

    def held_capture(
        video: Path, temporary: Path, _seek: float, *, processes: object = None
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        if os.path.samefile(video, a):  # ffmpeg is handed the pinned source's descriptor
            a_rendering.set()
            assert release_a.wait(10), "the test did not let A's capture go"
        else:
            release_b.wait(30)  # an unfinished batch: its end-of-batch refresh hasn't come
        return ""

    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", held_capture)
    pretend_scan = dry_run._scan

    def scanning(*arguments: Any, **keywords: Any) -> Any:
        # On entry to the dry run's scan: A's capture may finish now, if it can.
        release_a.set()
        deadline = time.monotonic() + 1.0
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return pretend_scan(*arguments, **keywords)

    monkeypatch.setattr(dry_run, "_scan", scanning)
    try:
        application._make_missing_stills()
        assert a_rendering.wait(10), "the batch did not start A's capture"
        item = application.session.library.find(a)
        assert item is not None and not capture.exists()
        count = len(installed.documents)
        finished: list[RemovalResult] = []

        application.remove_item_async(item, trash=True, finish=finished.append)
        spin_until(lambda: bool(finished) and _settled(application), 30, what="the removal")

        assert [result.committed for result in finished] == [True]
        assert not a.exists() and not capture.exists()
        assert application.session.library.find(capture) is None
        after = installed.since(count)
        assert after, "the removal's library was published"
        assert [missing for _document, missing in after if missing] == []
        assert all(str(capture) not in document for document, _missing_paths in after)
    finally:
        release_a.set()
        release_b.set()
        _close(application)


def test_s4_a_small_walk_budget_finds_what_survives_after_a_removal(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex S-4 in the application: c, past the budget before, is found after a is gone."""
    scenario = SCENARIOS["s4-walk-budget"](harness, harness.tmp_path)
    application = harness.application(scenario.session)
    assert isinstance(scenario.change, Path)
    item = application.session.library.find(scenario.change)
    assert item is not None
    finished: list[RemovalResult] = []

    application.remove_item_async(item, trash=True, finish=finished.append)
    spin_until(lambda: bool(finished) and _settled(application), 30, what="the removal")

    assert [result.committed for result in finished] == [True]
    found = {one.path.name for one in application.session.library.items}
    assert found == {"b.png", "c.png"}
