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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
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
        future = self._jobs.submit(ui_prefs.update, changes, self._path)
        future.add_done_callback(self._finished)

    def _finished(self, future: Future[ui_prefs.UiPrefsDocument]) -> None:
        try:
            future.result()
        except ui_prefs.UiPrefsReadOnlyError as error:
            message = str(error)
        except Exception as error:  # the worker boundary: report, never raise into GTK
            message = f"Window preferences were not saved: {error}"
        else:
            return

        def deliver() -> bool:
            if not self._closed:
                self._report(message)
            else:
                LOGGER.warning("%s", message)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    @property
    def busy(self) -> bool:
        """A change is waiting to be saved or being saved."""
        return bool(self._pending) or bool(self._timer)

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
