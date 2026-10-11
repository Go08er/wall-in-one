"""A health save that keeps failing the same way is said once, not on every status poll.

The status poll runs every two seconds. Each one that carries a wallpaper the
runtime couldn't play asks the health lane to save its marker in
``pairings.json`` (`Application._queue_runtime_health`), so whatever refuses
that save refuses it again two seconds later, for as long as the runtime keeps
reporting the wallpaper. `tests.test_read_only_profile` found it with the
state folder read-only: the same toast and log line every two seconds, without
end. These pin the rule the fix follows (`Application._say_health_failure_once`),
on the same real application, profile and permission bits:

* the same failure is one toast and one log line, however many polls meet it;
* it is said again once a save landed in between, and when what it says
  changes: this is no mute;
* the same holds for the two other things the poll's transaction can say, the
  lane's own failure and a publication that can't be installed, and for a
  process with no window, which saves on the caller's thread; a window that
  process opens later is told once too.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests import read_only_probe as ro  # noqa: E402
from tests.gtk_helpers import settle, spin_until  # noqa: E402
from tests.test_read_only_profile import (  # noqa: E402
    Ctx,
    _failed_wallpaper,
    _marker_saved,
    profile,  # noqa: F401 -- a fixture
    start,
    toolkit,  # noqa: F401 -- a fixture: GTK, once for the module
)
from tests.test_ui_runtime import FakeWindow, _attach  # noqa: E402
from wall_in_one import paths, runtime_config, runtime_health  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import pairings  # noqa: E402

#: What the health lane says when ``pairings.json`` doesn't take the marker.
REFUSED = "Could not save wallpaper playback availability"


class Runtime:
    """The runtime's socket: every verb acknowledged, ``status`` answered as scripted.

    Installed over the one `tests.read_only_probe.boot` leaves, so after `start`.
    """

    def __init__(self, ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
        self._ctx = ctx
        self._status = ""
        monkeypatch.setattr(client, "send_runtime", self._send)
        self.reports()

    def _send(self, verb: str, *_arguments: object, **_keywords: object) -> Response:
        return Response.success(self._status) if verb == "status" else Response.success()

    def reports(self, entry: int = 0, *, stale: bool = False) -> None:
        """From now on: Evening's ``entry`` couldn't be played, under the document on disk.

        ``stale``: under some earlier document, which the health lane leaves
        alone (it saves nothing for a status older than what is published).
        """
        status = _failed_wallpaper(self._ctx.application)
        assert status is not None
        reports = status["taboo_entries"]
        assert isinstance(reports, list) and len(reports) == 1
        reports[0]["entry_id"] = self._ctx.playlist("Evening").entries[entry].id
        if stale:
            status["config_generation"] = "0" * len(str(status["config_generation"]))
        self._status = json.dumps(status)


def poll(ctx: Ctx, times: int = 1) -> None:
    """``times`` status polls, each with the health save it asks for finished."""
    application = ctx.application
    for _poll in range(times):
        assert application.refresh_runtime_status_async()
        spin_until(
            lambda: not application._runtime_status_pending and ctx.probe.quiet(),
            ro.BOUND,
            what="a status poll and the health save it asks for",
        )
        settle(0.05)


def logged(ctx: Ctx) -> list[str]:
    """What the session log holds from the application, every time each was said."""
    return [e.text for e in ctx.application.activity_log.entries if e.source == "app"]


def said(ctx: Ctx, about: str) -> tuple[list[str], list[str]]:
    """The toasts and the log lines that mention ``about``."""
    return (
        [text for text in ctx.window.reports if about in text],
        [text for text in logged(ctx) if about in text],
    )


def test_a_refused_health_save_is_one_toast_and_one_log_line_over_many_polls(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six polls refused alike, and between them one that tries nothing: said once.

    The one between carries a status older than the published document, as a
    poll that found the compiler busy would: no save is tried, so it is not
    the save succeeding, and the refusal after it is not a new one.
    """
    with ro.read_only(profile.state):
        ctx = start(profile, "state", monkeypatch)
        try:
            runtime = Runtime(ctx, monkeypatch)
            poll(ctx, 3)
            runtime.reports(stale=True)
            poll(ctx)
            runtime.reports()
            poll(ctx, 3)
            toasts, lines = said(ctx, REFUSED)
            assert not ro.held(ctx.application)
        finally:
            ro.close(ctx.application)
    assert not _marker_saved(), "refused: nothing reached pairings.json"
    assert len(toasts) == 1 and ro.WHY.search(toasts[0]), toasts
    assert lines == toasts, "the log holds the same one line"


def test_the_same_refusal_is_said_again_after_a_save_that_landed(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused, then saved while the folder was writable, then refused again: said twice.

    The second refusal is for another wallpaper (the first one's marker is
    saved, so nothing would be written for it) and reads exactly as the first
    did: it names the file, not the wallpaper.
    """
    ctx = start(profile, "state", monkeypatch)
    try:
        runtime = Runtime(ctx, monkeypatch)
        with ro.read_only(profile.state):
            poll(ctx, 2)
            assert len(said(ctx, REFUSED)[0]) == 1
        poll(ctx)
        assert _marker_saved(), "writable again: the same poll's save landed"
        assert len(said(ctx, REFUSED)[0]) == 1, "and a save that lands says nothing"
        with ro.read_only(profile.state):
            runtime.reports(entry=1)
            poll(ctx, 2)
            # The first wallpaper again: its marker is there, so nothing is
            # written, and that is not the folder taking a save.
            runtime.reports(entry=0)
            poll(ctx)
            runtime.reports(entry=1)
            poll(ctx, 2)
            toasts, lines = said(ctx, REFUSED)
    finally:
        ro.close(ctx.application)
    assert len(toasts) == 2 and toasts[0] == toasts[1], toasts
    assert lines == toasts


def test_a_health_save_refused_for_another_reason_is_said(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The folder read-only, then the store's lock file unreadable as well: two things to say."""
    lock = pairings._mutation_lock_path(pairings.state_path())
    with ro.read_only(profile.state):
        ctx = start(profile, "state", monkeypatch)
        try:
            Runtime(ctx, monkeypatch)
            poll(ctx, 2)
            first, _lines = said(ctx, REFUSED)
            mode = lock.stat().st_mode & 0o777
            lock.chmod(0)
            try:
                poll(ctx, 2)
            finally:
                lock.chmod(mode)
            both, lines = said(ctx, REFUSED)
            poll(ctx, 2)
            after, _lines = said(ctx, REFUSED)
        finally:
            ro.close(ctx.application)
    assert len(first) == 1 and "could not write" in first[0], first
    assert len(both) == 2 and "mutation lock" in both[1], both
    assert all(ro.WHY.search(text) for text in both), both
    assert lines == both
    # The lock readable again, the folder still read-only: what is said changed back.
    assert after == [*both, first[0]], after


def test_the_health_lanes_own_failure_is_said_once(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transaction that fails outside its own refusals reaches the same report."""
    ctx = start(profile, "state", monkeypatch)
    try:
        Runtime(ctx, monkeypatch)

        def broken(*_arguments: object, **_keywords: object) -> object:
            raise RuntimeError("the status could not be matched to the library")

        monkeypatch.setattr(runtime_health, "taboo_inventory", broken)
        poll(ctx, 4)
        toasts, lines = said(ctx, "could not be matched")
        assert not ro.held(ctx.application)
    finally:
        ro.close(ctx.application)
    assert len(toasts) == 1, toasts
    assert lines == toasts


def test_a_publication_the_poll_cannot_install_is_said_once(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker saved; what the poll's transaction can't do is install the documents.

    An overrides file nothing uses lies beside ``runtime.toml`` and the
    read-only folder won't let it be removed, so every poll's install fails
    alike: said once, and kept as the publication problem. Once the folder
    takes it the problem is gone, and the same failure afterwards is new.
    """
    ctx = start(profile, "state", monkeypatch)
    application = ctx.application
    try:
        runtime = Runtime(ctx, monkeypatch)
        poll(ctx)
        assert _marker_saved()
        overrides = runtime_config.overrides_path(paths.runtime_config_path())
        assert not overrides.exists(), "this profile uses no override"
        overrides.write_text("schema_version = 1\n", encoding="utf-8")
        with ro.read_only(profile.state):
            runtime.reports()
            poll(ctx, 4)
            toasts, lines = said(ctx, overrides.name)
            standing = application.publication_problem
        poll(ctx)
        assert not overrides.exists() and application.publication_problem == ""
        assert len(said(ctx, overrides.name)[0]) == 1, "one that gets through says nothing"
        overrides.write_text("schema_version = 1\n", encoding="utf-8")
        with ro.read_only(profile.state):
            runtime.reports()
            poll(ctx, 2)
            again, _lines = said(ctx, overrides.name)
    finally:
        ro.close(application)
    assert len(toasts) == 1 and ro.WHY.search(toasts[0]), toasts
    assert lines == toasts and standing == toasts[0]
    assert again == [toasts[0], toasts[0]], again


def test_a_process_with_no_window_says_it_once_and_a_window_opened_later_is_told(
    profile: ro.Profile,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No window: the save runs on the caller's thread (`_adopt_runtime_status`), the log hears.

    A resident process ticks like that for as long as it runs. A window it
    opens later wasn't there to be told, so it is told, once.
    """
    with ro.read_only(profile.state):
        ctx = start(profile, "state", monkeypatch)
        application = ctx.application
        try:
            Runtime(ctx, monkeypatch)
            application._window = None
            for _tick in range(4):
                assert application.refresh_runtime_status()
            headless = [text for text in logged(ctx) if REFUSED in text]
            opened = FakeWindow()
            _attach(application, opened)
            poll(ctx, 3)
            lines = [text for text in logged(ctx) if REFUSED in text]
        finally:
            ro.close(application)
    assert not _marker_saved()
    assert len(headless) == 1 and ro.WHY.search(headless[0]), headless
    assert [text for text in opened.reports if REFUSED in text] == headless
    assert lines == headless * 2, "and the log holds the line for each"
