"""A not-durable warning reaches the window through the sink startup installs.

Two routes carry "was saved, but syncing its folder failed" to the window.
The authoring lane collects its own task's warnings and says them with that
task (`state_file.collecting_durability_warnings`). Every other worker (the
runtime lane, the Tidy lane, the new window's preferences) has no task to
attach them to, so `state_file.report_not_durable` hands them to the
process's sink, which `Application.do_startup` installs and `do_shutdown`
restores. Without it they only reached the log.

This drives the real application: startup, the first scan, one ``ctl``
setting that needs a new runtime document, and shutdown. The setting's own
warning arrives through the lane, the runtime document's through the sink;
nothing here installs a sink.
"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_next_editing import first_scan, reports_of  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    control,
    sandboxed_runtime,
    settled,
    toolkit,
)
from wall_in_one.library import state_file  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

SAVED_BUT = "was saved, but syncing its folder failed"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def _sync_fails_for(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[str]:
    """Fail the folder sync after any of ``names`` is renamed into place, and only those."""
    real = state_file.fsync_parent
    failed: list[str] = []

    def sync(path: Path) -> None:
        if Path(path).name in names:
            failed.append(Path(path).name)
            raise OSError(errno.EIO, "injected post-replace directory sync failure")
        real(path)

    monkeypatch.setattr(state_file, "fsync_parent", sync)
    return failed


def test_a_runtime_lane_warning_reaches_the_window_through_the_startup_sink(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = state_file._durability_sink
    application = Application(ui="next")
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        failed = _sync_fails_for(monkeypatch, "settings.toml", "runtime.toml")
        heard = len(said)
        made = control(requests, "cycle-interval", "120")

        def both_said() -> bool:
            new = said[heard:]
            return (
                made.done()
                and settled(application)
                and any("settings.toml" in m for m in new)
                and any("runtime.toml" in m for m in new)
            )

        yield "the setting, its runtime document and both warnings", both_said
        reply = made.result()
        assert reply.ok and reply.message.startswith("cycle-interval 120"), reply.message
        assert SAVED_BUT in reply.message, reply.message
        assert application.settings.cycle_interval == 120
        assert failed == ["settings.toml", "runtime.toml"], failed
        new = said[heard:]
        # The lane's warning, said once with its task.
        assert sum("settings.toml" in m for m in new) == 1, new
        # The runtime lane's, through the sink startup installed: once, as such.
        (published,) = [m for m in new if "runtime.toml" in m]
        assert published.startswith(f"runtime.toml {SAVED_BUT}"), published
        assert not any("nothing changed" in m for m in new), new
        assert runtime.heard("reload"), "and the lane reloaded what it published"
        yield from finish(application, window, lanes)

    try:
        run(application, scenario(), lanes)
    finally:
        requests.shutdown(wait=True)
    assert state_file._durability_sink is before, "shutdown put back the sink it replaced"
