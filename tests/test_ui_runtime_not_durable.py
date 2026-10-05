"""The runtime lane installs a pair published before its folder sync failed.

`runtime_config.update` renames ``runtime-overrides.toml`` and then
``runtime.toml`` into place and syncs their folder; a failure of that sync
raises `runtime_config.RuntimeConfigNotDurableError` with the new pair
already live. The lane treated it as a failed publication: it never sent
``reload`` nor counted the document, and the next compile found nothing to
change, so only the service's file watcher ever applied it. It is installed
like any other change now, with the inventory check before it unchanged.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_published_not_durable import SAVED_BUT, _sync_fails_for  # noqa: E402
from tests.test_ui_runtime import (  # noqa: E402,F401
    FakeWindow,
    _application,
    _attach,
    _close,
    toolkit,
)
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import state_file  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402


def test_the_runtime_lane_reloads_a_pair_published_before_the_sync_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    path = tmp_path / "library" / "paper.png"
    item = MediaItem(path=path, kind=Kind.STILL, size=1, mtime=1)
    application.session.adopt_library(Library(roots=(path.parent,), items=(item,)))
    sourced = replace(application.settings, roots=(path.parent,))
    config.save(sourced)
    application._settings = sourced
    application.session.update_settings(sourced, rescan_library=False)
    # What `Application.do_startup` installs: a warning from a worker reaches the window.
    monkeypatch.setattr(
        state_file,
        "_durability_sink",
        getattr(application, "_not_durable", application.window_report),
        raising=False,
    )
    calls: list[str] = []
    lane: list[int] = []

    def send_runtime(verb: str, *_arguments: object, **_keywords: object) -> Response:
        calls.append(verb)
        lane.append(threading.get_ident())
        return Response.success(verb)

    monkeypatch.setattr(client, "send_runtime", send_runtime)
    failed = _sync_fails_for(monkeypatch, "runtime.toml")
    generation = application._runtime_document_generation
    try:
        assert application._publish_runtime_async()
        spin_until(
            lambda: not application._runtime_compile_pending and bool(window.reports),
            timeout=5,
        )

        assert failed == [paths.runtime_config_path()], (window.reports, calls)
        assert paths.runtime_config_path().is_file(), "the new pair is live"
        # Before: no reload at all, and the warning arrived as a failure.
        assert calls[:1] == ["reload"], "the lane reloads what it published"
        assert calls.count("reload") == 1
        assert lane[0] != threading.get_ident()
        assert application._runtime_document_generation == generation + 1
        assert any(SAVED_BUT in report for report in window.reports)
        # The next compile has nothing to change, so nothing is reloaded twice.
        assert application._publish_runtime_async()
        spin_until(lambda: not application._runtime_compile_pending, timeout=5)
        assert calls.count("reload") == 1
    finally:
        _close(application)
