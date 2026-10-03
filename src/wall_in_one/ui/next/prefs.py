"""The new window's preferences in ``ui.toml``, saved only when the user changes one.

`UiPrefsKeeper` reads ``ui.toml`` once, when the window opens, and from then
on holds the preferences in memory. It writes only on an explicit preference
change -- the window style, an opacity dial, frost, the thumbnail size --
never on startup or while idle, and there is no unconditional save on close:
only a change the user made that is still pending is flushed then (`close`).
Nothing else lives here: no last page, no window size.

* Writes go to `wall_in_one.ui_prefs.update` on one worker thread, so the
  file lock and the atomic replace never block GTK. Only the fields that
  changed are written, rebased on the file as it is then.
* A dial being dragged changes the window live but is saved once, a moment
  after it stops (``delay_ms``). A change still waiting when the window
  closes is saved then: it was the user's.
* A file this build must not write (a newer version, unreadable, malformed)
  is never touched; ``read_only`` says why, and the window turns those
  controls off.
* Every save comes back as what is durable. A failed save (the version
  bump's backup can't be written, say) reports why, and the keeper returns
  to what ui.toml really holds, read back on the worker; a successful one
  adopts the saved file. Subscribers (`subscribe`) then show that, so a row
  never keeps a choice that was not saved, and choosing it again saves
  again. Only the latest save reconciles, and only once nothing newer is
  waiting: an earlier save finishing never reverts a later choice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import gi

gi.require_version("GLib", "2.0")

from gi.repository import GLib

from wall_in_one import ui_prefs
from wall_in_one.ui_prefs import UiPrefs

LOGGER = logging.getLogger(__name__)
#: How long a dial must rest before its value is saved.
DIAL_SAVE_DELAY_MS: Final = 600
#: The interface a start without ``--ui`` builds, as both windows offer it.
INTERFACE_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    ("classic", "Classic"),
    ("next", "New (preview)"),
)
INTERFACE_NOTE: Final = "Takes effect the next time Wall-in-One starts."
#: Whether the next start's window may render on the GPU, as both windows offer it.
GPU_NOTE: Final = "Takes effect the next time Wall-in-One starts. Off uses software rendering."


@dataclass(frozen=True, slots=True)
class _Saved:
    """One finished save, as the worker found ui.toml afterwards."""

    #: Which flush this was (`UiPrefsKeeper._revision` when it was handed over).
    revision: int
    #: What ui.toml holds now: the saved document, or the file read back.
    prefs: UiPrefs
    #: Why this build must not write ui.toml, as read back; empty when it may.
    read_only: str = ""
    #: Why the save failed, or empty.
    error: str = ""


def _save(changes: dict[str, Any], path: Path | None, revision: int) -> _Saved:
    """Save on the worker and say what is durable; never raises.

    A failure reads the file back here too, so GTK never waits on ui.toml
    even when a completion is delivered on its own thread.
    """
    try:
        saved = ui_prefs.update(changes, path)
    except ui_prefs.UiPrefsReadOnlyError as error:
        message = str(error)
    except Exception as error:  # the worker boundary: report, never raise
        message = f"Window preferences were not saved: {error}"
    else:
        return _Saved(revision, saved.prefs)
    durable = ui_prefs.load(path)
    return _Saved(revision, durable.prefs, durable.read_only, message)


class UiPrefsKeeper:
    """The window's preferences: read once, written on explicit changes only."""

    def __init__(self, report: Callable[[str], None], path: Path | None = None) -> None:
        self._report = report
        self._path = path
        document = ui_prefs.load(path)
        self.prefs: UiPrefs = document.prefs
        #: Why this build must not write ui.toml, or empty.
        self.read_only: str = document.read_only
        self._pending: dict[str, Any] = {}
        self._timer = 0
        self._jobs: ThreadPoolExecutor | None = None
        self._closed = False
        #: Writes handed to the worker, for tests and shutdown.
        self.saves = 0
        #: The latest flush's number: only its completion may reconcile.
        self._revision = 0
        #: Saves handed to the worker whose result GTK has not adopted yet.
        self._in_flight = 0
        self._subscribers: list[tuple[object, Callable[[], None]]] = []

    def subscribe(self, subscriber: Callable[[], None]) -> Callable[[], None]:
        """Call ``subscriber`` after ``prefs`` or ``read_only`` changed to what is durable.

        Returns its unsubscribe. Called on GTK's thread.
        """
        token = object()
        self._subscribers.append((token, subscriber))

        def unsubscribe() -> None:
            self._subscribers[:] = [entry for entry in self._subscribers if entry[0] is not token]

        return unsubscribe

    def change(self, *, delay_ms: int = 0, **fields: Any) -> bool:
        """Adopt new values now and save them (after ``delay_ms`` of quiet).

        False, with nothing saved, when ui.toml is read-only here.
        """
        updated = replace(self.prefs, **fields).validated()
        if updated == self.prefs:
            return True
        if self.read_only:
            return False
        self.prefs = updated
        self._pending.update(
            {
                name: getattr(updated, name)
                for name in fields
                if name in UiPrefs.__dataclass_fields__
            }
        )
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if delay_ms > 0 and not self._closed:
            self._timer = GLib.timeout_add(delay_ms, self._flush_later)
        else:
            self.flush()
        return True

    def _flush_later(self) -> bool:
        self._timer = 0
        self.flush()
        return GLib.SOURCE_REMOVE

    def flush(self) -> None:
        """Hand every waiting change to the worker now."""
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if not self._pending:
            return
        changes, self._pending = self._pending, {}
        if self._jobs is None:
            self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui-prefs")
        self.saves += 1
        self._revision += 1
        self._in_flight += 1
        future = self._jobs.submit(_save, changes, self._path, self._revision)
        future.add_done_callback(self._finished)

    def _finished(self, future: Future[_Saved]) -> None:
        """Hand a finished save to GTK (this may run on the worker, or on GTK)."""
        saved = future.result()  # _save never raises

        def deliver() -> bool:
            self._adopt(saved)
            return GLib.SOURCE_REMOVE

        # Ends a choice in flight that a row shows: above redraw, never starved.
        GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _adopt(self, saved: _Saved) -> None:
        """Report a failure, then show what is durable unless something newer is coming."""
        self._in_flight -= 1
        if saved.error:
            if not self._closed:
                self._report(saved.error)
            else:
                LOGGER.warning("%s", saved.error)
        if saved.revision != self._revision or self._pending or self._timer:
            return  # a later save is in flight or waiting; it reconciles
        if saved.prefs == self.prefs and saved.read_only == self.read_only:
            return
        self.prefs = saved.prefs
        self.read_only = saved.read_only
        if self._closed:
            return
        for _token, subscriber in tuple(self._subscribers):
            subscriber()

    @property
    def busy(self) -> bool:
        """A change is waiting to be saved, or a save has not come back to GTK yet."""
        return bool(self._pending) or bool(self._timer) or self._in_flight > 0

    def reopen(self) -> None:
        """Tell subscribers about saves again, after `close`: the window is back."""
        self._closed = False

    def close(self, *, wait: bool = False) -> None:
        """Save what the user changed and is still waiting, then stop the worker.

        Never writes anything that was not changed: closing an untouched
        window leaves ui.toml exactly as it was (or absent). Without ``wait``
        the last write finishes on its own thread (interpreter exit waits
        for it); tests wait here instead.
        """
        self.flush()
        self._closed = True
        jobs, self._jobs = self._jobs, None
        if jobs is not None:
            jobs.shutdown(wait=wait)
