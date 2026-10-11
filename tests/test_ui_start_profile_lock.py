"""A start whose repair could not run rests the gate, with the reason (review AC-13).

The start gives the repair of dangling playlist references to the authoring
lane, whose guard takes the profile-wide lock first. When that guard refused
(`MigrationError`), the failure was said once and nothing moved the gate: it
stayed "checking", every change was answered "current-profile authoring is
still opening; retry shortly", and no library was loaded, until a restart
that did the same.

A lock file that is there but can't be opened for writing no longer refuses
at all (`tests/test_profile_lock_unwritable.py`, and the read-only profile's
start in `tests/test_read_only_profile.py`). What still refuses is a lock
that can't be taken in any way (not there in a folder that can't take one,
or not readable either), and one somebody holds past the wait. Then the gate
rests at "repair", which Try again and a restart run once more; every
refusal carries the reason and the file; and the library stays unloaded,
because without the lock nothing says an import isn't writing the profile.
"""

from __future__ import annotations

import fcntl
import functools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests import read_only_probe as ro  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.started import started_application  # noqa: E402
from tests.test_read_only_profile import profile  # noqa: E402
from tests.test_ui_next_first_run import _no_notice_of_its_own, _notice  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _attach  # noqa: E402
from wall_in_one import legacy_migration  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next import window as next_window  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

__all__ = ["profile"]  # the read-only probe's fixture, used here by name

STILL_OPENING = "still opening"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


def _lock() -> Path:
    marker = legacy_migration.marker_path()
    return marker.with_name(f".{marker.name}.mutation.lock")


@pytest.fixture
def services(
    profile: ro.Profile,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[Application, FakeWindow]]]:
    """Resident processes over the probe's healthy profile, started through the real gate."""
    ro.enter(profile, monkeypatch)
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    for name in ("set_wallpaper", "set_scheme", "set_mode", "reload_config", "apply_templates"):
        monkeypatch.setattr(noctalia, name, lambda *_a, **_k: None)
    made: list[tuple[Application, FakeWindow]] = []
    try:
        yield made
    finally:
        _lock().parent.chmod(0o700)
        if _lock().exists():
            _lock().chmod(0o600)
        for application, _window in made:
            ro.close(application)


def _start(made: list[tuple[Application, FakeWindow]]) -> tuple[Application, FakeWindow]:
    """A ``--service`` process at the moment its start opens the gate."""
    application = Application(service=True, ui="next")
    window = FakeWindow()
    _attach(application, window)
    application._make_missing_stills = lambda: None  # type: ignore[method-assign]
    made.append((application, window))
    application._open_authoring_after_migration()
    spin_until(
        lambda: application.authoring_ready or application.authoring_gate != "checking",
        ro.BOUND * 2,
        what="the gate to open or to rest",
    )
    return application, window


def _rested(application: Application, window: FakeWindow, reason: str, *, names: str = "") -> None:
    """At "repair" with its reason (and what it ``names``): said once, and with every refusal."""
    assert application.authoring_gate == "repair" and not application.authoring_ready
    assert reason in application.authoring_gate_reason
    assert names in application.authoring_gate_reason, "the file is named"
    said = [text for text in window.reports if reason in text]
    assert len(said) == 1, window.reports
    first = len(window.reports)
    logged = len(application.activity_log.entries)
    for verb, argument in (("favourite", "nowhere.png"), ("shuffle", "toggle")):
        reply = ro.ctl(application, verb, argument)
        assert not reply.ok and reply.kind == "authoring-repair-required", reply
        assert reason in reply.message and names in reply.message, reply.message
        assert STILL_OPENING not in reply.message
    assert len(window.reports) == first, "a ctl refusal is its caller's, not another toast"
    assert len(application.activity_log.entries) == logged, "nor another line in the log"
    assert len(application.session.library.items) == 0, "rested without a library"


def _opens(application: Application) -> None:
    """Try again, with nothing in the way: open, the library loaded, nothing left over."""
    assert application.retry_authoring_repair()
    spin_until(
        lambda: application.authoring_ready and ro.quiet(application),
        ro.BOUND * 2,
        what="Try again to open the gate",
    )
    assert application.authoring_gate == "open" and application.authoring_gate_reason == ""
    assert application._authoring_gate_reason == ""
    assert application.session.library.items, "the library is loaded"
    assert not application.retry_authoring_repair(), "nothing more to try"


@pytest.mark.parametrize("how", ["not readable either", "not there, and can't be made"])
def test_a_lock_that_cannot_be_taken_rests_the_gate_with_its_reason(
    services: list[tuple[Application, FakeWindow]], how: str
) -> None:
    """Mode 000, or missing from a folder that can't take a new file: no way
    to take it, and no way to know nobody holds it."""
    ro.skip_as_root()
    with legacy_migration.unattended_transaction():
        pass
    folder = _lock().parent
    if how == "not readable either":
        _lock().chmod(0o000)
    else:
        _lock().unlink()
        folder.chmod(0o555)
    try:
        application, window = _start(services)
        _rested(application, window, "Permission denied", names=_lock().name)

        # A restart is the same; and Try again while it can't is the same again.
        again, again_window = _start(services)
        _rested(again, again_window, "Permission denied", names=_lock().name)
        assert again.retry_authoring_repair()
        spin_until(lambda: again.authoring_gate == "repair", ro.BOUND * 2, what="it to rest again")
        assert "Permission denied" in again.authoring_gate_reason
    finally:
        folder.chmod(0o700)
        if _lock().exists():
            _lock().chmod(0o600)

    _opens(again)
    # And a start after it can be taken again knows nothing of any of this.
    fresh, fresh_window = _start(services)
    assert fresh.authoring_ready and fresh.authoring_gate_reason == ""
    assert not [text for text in fresh_window.reports if "Permission denied" in text]


def test_a_lock_held_past_its_wait_rests_the_gate_and_try_again_opens_it(
    services: list[tuple[Application, FakeWindow]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held by someone is waited for (a minute, here a moment); past that the
    gate rests, never "still opening" with nothing running."""
    with legacy_migration.unattended_transaction():
        pass
    waited = functools.partial(legacy_migration.unattended_transaction, timeout=0.3)
    monkeypatch.setattr(legacy_migration, "unattended_transaction", waited)
    descriptor = os.open(_lock(), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        application, window = _start(services)
        _rested(application, window, "timed out")
    finally:
        os.close(descriptor)
    _opens(application)


def test_the_new_windows_notice_gives_the_reason_and_tries_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ro.skip_as_root()
    sandboxed_runtime(monkeypatch, tmp_path)
    application = started_application(ui="next")
    window = NextWindow(application, application.settings)
    application._window = window
    window.present()
    with legacy_migration.unattended_transaction():
        pass
    try:
        # The start again, with the lock out of reach.
        application._authoring_migration_ready = False
        application._authoring_repair_started = False
        _lock().chmod(0o000)
        application._open_authoring_after_migration()
        spin_until(lambda: _notice(window) == next_window.REPAIR, timeout=ro.BOUND)
        title = window.banner.get_title()
        assert title.startswith(next_window.NOT_OPENED) and "Permission denied" in title, title
        assert title != next_window.REPAIR_NEEDED, "not the dangling-references notice"
        assert window.banner.get_button_label() == "Try Again"

        _lock().chmod(0o600)
        window.banner.emit("button-clicked")
        spin_until(lambda: application.authoring_gate == "open", timeout=ro.BOUND)
        spin_until(lambda: _no_notice_of_its_own(window), timeout=ro.BOUND)
        assert application.authoring_gate_reason == ""
    finally:
        _lock().chmod(0o600)
        application._window = None
        window.destroy()
