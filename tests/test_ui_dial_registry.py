"""The registry of waiting numbers, `dial_rows._PENDING`, and its `flush_all` (review r13b, N-f).

Every settle timer that waits is registered, so that a window that closes, or an application
that quits, can hand each number on at once (`flush_all`). One hand-on that raised used to stop
the rest: they stayed armed for a loop that was ending, and the close-request that called it
never got as far as forgetting its window. Each is now run, and one that raises is logged.

Every `GLib.source_remove` here must name a source that exists.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib, Gtk  # noqa: E402

from tests.test_ui_renderer_dials import toolkit  # noqa: E402,F401
from wall_in_one.ui import dial_rows  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402

_BAD_REMOVALS: list[int] = []


@pytest.fixture(autouse=True)
def checked_source_remove(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = GLib.source_remove
    context = GLib.MainContext.default()

    def checked(source_id: int) -> bool:
        if context.find_source_by_id(source_id) is None:
            _BAD_REMOVALS.append(source_id)
        return real(source_id)

    _BAD_REMOVALS.clear()
    dial_rows._PENDING.clear()
    monkeypatch.setattr(GLib, "source_remove", checked)
    yield
    left = dict(dial_rows._PENDING)
    for _owner, _key, cancel, _flush in tuple(left.values()):
        cancel()  # nothing of this test may reach the next one
    assert not _BAD_REMOVALS, f"GLib.source_remove on sources that do not exist: {_BAD_REMOVALS}"
    assert not left, f"left in the registry: {left}"


def _drain(milliseconds: int) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.002)


def _row(owner: object, key: str, calls: list[str], *, raises: bool = False) -> Any:
    settling = dial_rows.Settling(owner)

    def callback() -> None:
        calls.append(f"{owner}/{key}")
        if raises:
            raise RuntimeError(f"{owner}/{key} failed")

    call, cancel = dial_rows._settled(callback, settling, key)
    return SimpleNamespace(call=call, cancel=cancel, settling=settling)


# -- the registry follows its timers ---------------------------------------------------------


def test_fire_cancel_flush_each_leave_the_registry_empty() -> None:
    calls: list[str] = []
    fired, cancelled, flushed = (_row("a", key, calls) for key in ("fire", "cancel", "flush"))
    for row in (fired, cancelled, flushed):
        row.call()
        row.call()  # a second nudge replaces the first timer
    assert len(dial_rows._PENDING) == 3
    cancelled.cancel()
    flushed.settling.flush()
    _drain(dial_rows.SETTLE_MS + 200)
    assert calls == ["a/flush", "a/fire"], calls
    # Every exit again, on sources that are gone: no stale source_remove.
    for row in (fired, cancelled, flushed):
        row.cancel()
        row.settling.flush()
    dial_rows.flush_all()
    dial_rows.cancel_pending("a")
    assert calls == ["a/flush", "a/fire"], calls


def test_flush_all_twice_hands_each_number_on_once() -> None:
    calls: list[str] = []
    rows = [_row("a", "x", calls), _row("b", "y", calls)]
    for row in rows:
        row.call()
    dial_rows.flush_all()
    dial_rows.flush_all()
    _drain(dial_rows.SETTLE_MS + 200)
    for row in rows:
        row.settling.flush()
    assert sorted(calls) == ["a/x", "b/y"], calls


def test_a_hand_on_that_resets_drops_the_later_number() -> None:
    """A reset run by the first hand-on (`cancel_pending`) is the last word for its wallpaper."""
    calls: list[str] = []
    settling = dial_rows.Settling("a")

    def reset() -> None:
        calls.append("a/reset")
        dial_rows.cancel_pending("a")

    first, _cancel = dial_rows._settled(reset, settling, "x")
    later = _row("a", "y", calls)
    first()
    later.call()
    dial_rows.flush_all()
    _drain(dial_rows.SETTLE_MS + 200)
    assert calls == ["a/reset"], calls


# -- when one hand-on raises (N-f) -----------------------------------------------------------


@pytest.mark.parametrize("raising", [True, False], ids=["one-raises", "control"])
def test_flush_all_hands_on_every_other_number_when_one_raises(
    raising: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """Before: the RuntimeError left `flush_all` at the first, and the other two waited on."""
    calls: list[str] = []
    rows = [
        _row("a", "first", calls, raises=raising),
        _row("b", "second", calls),
        _row("c", "third", calls),
    ]
    for row in rows:
        row.call()
    with caplog.at_level(logging.ERROR, logger=dial_rows.__name__):
        try:
            dial_rows.flush_all()
        except RuntimeError as error:
            raised: RuntimeError | None = error
        else:
            raised = None
    handed_now = sorted(calls)
    for row in rows:
        row.cancel()  # the timers of a window that has gone may never fire
    assert raised is None, f"flush_all raised: {raised}"
    assert handed_now == ["a/first", "b/second", "c/third"], handed_now
    logged = [record for record in caplog.records if record.name == dial_rows.__name__]
    assert len(logged) == (1 if raising else 0), logged
    if raising:
        assert logged[0].exc_info is not None and "a/first failed" in str(logged[0].exc_info[1])


@pytest.mark.parametrize("raising", [True, False], ids=["one-raises", "control"])
def test_close_request_forgets_the_window_even_when_a_hand_on_raises(raising: bool) -> None:
    """`Application._on_close_request` hands on before it drops `self._window`.

    Before: the error left it there, so a later activation presented the destroyed window.
    """
    calls: list[str] = []
    row = _row("a", "x", calls, raises=raising)
    row.call()
    window = Gtk.Window()
    host = SimpleNamespace(
        _window=window,
        _window_generation=0,
        _legacy_migration_generation=0,
        _legacy_migration_future=None,
        _legacy_migration_retry_probe=False,
        _legacy_migration_prompt=None,
        _legacy_migration_deferred=False,
        _legacy_migration_prompted=True,
        _stop_runtime_status_timer=lambda: None,
        _hand_on_waiting_numbers=dial_rows.flush_all,
    )
    with contextlib.suppress(RuntimeError):  # before: it raised, past its own cleanup
        Application._on_close_request(host, window)  # type: ignore[arg-type]
    window.destroy()
    assert calls == ["a/x"], calls
    assert host._window is None, "the destroyed window is still the application's window"
