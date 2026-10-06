"""A write that landed is never reported as nothing changed when what follows it fails.

An authoring task writes on its worker (`work`), then adopts the result on
GTK (`finish`). An exception from `finish` came back exactly like one from
`work`: as a plain failure, which for a window caller reads "... was not
saved; nothing changed", and nothing published the write. The known case is
``ctl select``: the Quick choice is saved, then `Session.use_playlist`
refuses because, between the write and its adoption, a health marker made
the chosen wallpaper unplayable. The reply now says it was saved and why it
couldn't go on, and the saved Quick choice is published.

Driven through the real application, with a real marker: written on GTK by
the pairings store between the select's write and its adoption, as a health
result landing then would.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    control,
    sandboxed_runtime,
    settled,
    toolkit,
)
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.library import pairings, playlists  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def _marked_between(application: Application, dawn: str) -> Callable[..., object]:
    """`playlists.Store.set_singleton`, then a health marker for ``dawn`` lands on GTK."""
    real_set_singleton = playlists.Store.set_singleton

    def saved_then_marked(store: playlists.Store, *arguments: object, **keywords: object) -> object:
        chosen = real_set_singleton(store, *arguments, **keywords)  # type: ignore[arg-type]
        marked = threading.Event()

        def mark() -> bool:
            # A health result landing on GTK between the write and its adoption.
            (item,) = [i for i in application.session.library.items if str(i.path) == dawn]
            application.session.pairings.mark_borked(item, "renderer crashed", "runtime")
            marked.set()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(mark)
        assert marked.wait(5)
        return chosen

    return saved_then_marked


def test_a_select_saved_then_refused_says_so_and_publishes_the_choice(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[object] = []
    dawn = wallpaper(tmp_path, "dawn.png")
    saved_then_marked = _marked_between(application, dawn)

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        monkeypatch.setattr(playlists.Store, "set_singleton", saved_then_marked)
        generation = application._runtime_compiled_generation
        heard = len(said)
        selected = control(requests, "select", dawn)
        yield "the select to be answered", lambda: selected.done() and settled(application)

        reply = selected.result()
        assert not reply.ok, "it could not be played"
        # Before: just the refusal, as if nothing had been saved.
        assert reply.message.startswith("Saved, but"), reply.message
        assert "every item is marked Borked" in reply.message, reply.message
        assert "nothing changed" not in reply.message
        chosen = playlists.Store.open().get(QUICK_CHOICE_ID)
        assert chosen is not None and [e.source for e in chosen.entries] == [dawn]
        assert application.session.playlists.get(QUICK_CHOICE_ID) is not None
        # What was saved is published: a compile ran after it, with it in.
        assert application._runtime_compiled_generation > generation
        assert QUICK_CHOICE_ID in paths.runtime_config_path().read_text()
        identity = pairings.Identity.of(
            next(i for i in application.session.library.items if str(i.path) == dawn)
        )
        assert application.session.pairings.health(identity).is_borked
        assert not any("nothing changed" in m for m in said[heard:]), said[heard:]
        yield from finish(application, window, lanes)

    try:
        run(application, scenario(), lanes)
    finally:
        requests.shutdown(wait=True)


def test_the_windows_quick_choice_saved_then_refused_says_so_and_publishes_it(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window's own Quick choice handles the refusal itself, and now publishes."""
    application = Application(ui="next")
    lanes: list[object] = []
    dawn = wallpaper(tmp_path, "dawn.png")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        monkeypatch.setattr(playlists.Store, "set_singleton", _marked_between(application, dawn))
        generation = application._runtime_compiled_generation
        heard = len(said)
        (item,) = [i for i in application.session.library.items if str(i.path) == dawn]
        assert application.play_item_async(item)
        yield "the choice to land", lambda: settled(application) and len(said) > heard

        (message,) = said[heard:]
        assert message.startswith("Saved, but it couldn't be applied:"), message
        assert "every item is marked Borked" in message
        # Before: said as the refusal alone, and never published.
        assert application._runtime_compiled_generation > generation
        assert QUICK_CHOICE_ID in paths.runtime_config_path().read_text()
        assert not application._quick_choice_pending
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_a_settings_callback_that_fails_after_the_save_is_not_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller's own follow-up failing is that caller's: the batch was saved, and goes on."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    application = Application(ui="classic")
    application._authoring_migration_ready = True
    reports: list[str] = []
    monkeypatch.setattr(Application, "window_report", lambda _self, m: reports.append(m))
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    followed: list[float] = []

    def follow_up(settings: config.Settings) -> None:
        raise RuntimeError("the page could not show it")

    try:
        assert application.update_settings_async(opacity=0.8, on_success=follow_up)
        assert application.update_settings_async(
            opacity=0.6, on_success=lambda saved: followed.append(saved.opacity)
        )
        spin_until(lambda: not application._settings_authoring_running and bool(followed))

        assert config.load_strict().opacity == 0.6, "both saves landed, the tail too"
        assert application.settings.opacity == 0.6
        assert followed == [0.6]
        # Before: "Settings were not saved; nothing changed: ..." for a saved batch.
        assert not any("nothing changed" in r for r in reports), reports
        assert any(r.startswith("Settings were saved, but") for r in reports), reports
    finally:
        application._runtime_shutdown = True
        jobs = application._authoring_jobs
        application._shutdown_authoring_jobs()
        if jobs is not None:
            jobs.shutdown(wait=True, cancel_futures=True)
        application._stills.shutdown()
        application.session.shutdown()


def test_a_window_task_whose_adoption_raises_is_said_saved_not_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane's contract for every window caller, whatever its finish does."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    application = Application(ui="classic")
    application._authoring_migration_ready = True
    reports: list[str] = []
    monkeypatch.setattr(Application, "window_report", lambda _self, m: reports.append(m))
    published: list[bool] = []

    def publish() -> bool:
        published.append(True)
        return True

    monkeypatch.setattr(application, "_publish_runtime_for_context", publish)
    store = application.session.playlists
    failures: list[str] = []

    def adopt(_made: object) -> None:
        raise RuntimeError("the page could not take it")

    try:
        assert application.authoring_action_async(
            lambda: store.create("Evening"),
            adopt,
            failure=lambda message: failures.append(f"not saved; nothing changed: {message}"),
        )
        spin_until(lambda: not application._authoring_active and bool(reports))

        assert playlists.Store.open().by_name("Evening") is not None, "the write landed"
        assert failures == [], "the caller's 'nothing changed' is never said for a saved write"
        assert reports == ["Saved, but it couldn't be applied: the page could not take it"]
        assert published, "what was saved is published"
    finally:
        application._runtime_shutdown = True
        jobs = application._authoring_jobs
        application._shutdown_authoring_jobs()
        if jobs is not None:
            jobs.shutdown(wait=True, cancel_futures=True)
        application._stills.shutdown()
        application.session.shutdown()
