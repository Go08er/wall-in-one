"""Taking stills from videos in the background, so none is ever missing.

`library.stills` can make a still; `session` makes one at the moment dynamics
are switched off, for the video that would otherwise go dead. That is the
narrowest possible fix and it leaves the rest of the library as it was: every
other video keeps its "no still" badge, keeps dropping out of the rotation when
dynamics are off, and keeps giving Noctalia a palette generated from whatever
was on screen before it.

So this does the rest, on a pool, the way `ui.thumbnails` does thumbnails. The
two are deliberately the same shape -- work off-thread, deliver through
`GLib.idle_add` -- but not the same pool: a still is a full-resolution frame
out of a 4K video and takes about a second, against a thumbnail's third of one,
and letting those queue behind each other would leave the grid blank while the
stills ground away.

One worker, not four. Every job here is ffmpeg decoding a large video, so the
disk is the limit rather than the cores, and a library of forty videos should
not start forty of them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Final

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib

from wall_in_one import worker_processes
from wall_in_one.library import stills, workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.ui import monitors
from wall_in_one.wallpaper import outputs, scenes

#: Deliberately one. See the module docstring.
MAX_WORKERS: Final = 1

#: Called once, on the main thread, after a batch has made at least one still.
#: The argument is how many were made, so a caller can decide whether a rescan
#: is worth doing.
Callback = Callable[[int], None]
FailureCallback = Callable[[str], None]
LOGGER = logging.getLogger(__name__)


def _never() -> bool:
    return False


def _engine_capture(item: MediaItem, previews: bool) -> bool:
    """Whether ``item``'s still is the scene engine's capture, which needs a measured display.

    Not for one drawn from its own preview (`workshop.preview_still`).
    """
    return item.kind is Kind.SCENE and not workshop.preview_still(item, previews)


def _refusal(folder: Path, count: int, reason: str) -> str:
    """What is said for ``count`` wallpapers whose still ``folder`` would not take."""
    wallpapers = "1 wallpaper" if count == 1 else f"{count} wallpapers"
    return (
        f"No still could be made for {wallpapers}: {folder} cannot be written ({reason}). "
        "Wall-in-One tries again the next time it starts"
    )


def _captures_failed(failures: list[tuple[str, str]]) -> str:
    """What is said for the scenes (and presets) the engine gave no still for."""
    count = len(failures)
    wallpapers = (
        "1 Wallpaper Engine wallpaper" if count == 1 else f"{count} Wallpaper Engine wallpapers"
    )
    name, reason = failures[0]
    more = f" (and {count - 1} more)" if count > 1 else ""
    return (
        f"linux-wallpaperengine gave no still for {wallpapers}: {name}{more}: {reason}. "
        "Without a still each stays out of playlists; pick one for it, or Wall-in-One "
        "tries again the next time it starts"
    )


class StillMaker:
    """Fills in the missing stills for a library, off the main thread."""

    def __init__(
        self,
        max_workers: int = MAX_WORKERS,
        *,
        report: FailureCallback | None = None,
        note: FailureCallback | None = None,
        displays: Callable[[], tuple[outputs.Output, ...]] | None = None,
        web_allowed: Callable[[], bool] | None = None,
        previews: Callable[[], bool] | None = None,
    ) -> None:
        """``report`` tells a person now (the window); ``note`` only keeps a record (the log).

        ``displays`` is the window's own view of the screens, GTK's monitors
        (`ui.monitors.snapshot`) unless given: taken on the main thread when a
        job is handed off, it is what a scene still is sized for when the
        wallpaper service has no snapshot to give.

        ``web_allowed`` says whether ``allow_web_wallpapers`` is on, asked afresh
        at every step. Without it web wallpapers are off: no still is made for
        one and none of its files is opened. While they are off a web page is not
        remembered as attempted, so turning them on makes theirs.

        ``previews`` says whether `config.Settings.preview_stills` is on, asked
        afresh at every step: a web page (or a preset of one) gets an automatic still only
        then (`workshop.preview_still`), drawn from its own preview. Without it, none.
        """
        self._web_allowed = web_allowed if web_allowed is not None else _never
        self._previews = previews if previews is not None else _never
        self._displays = displays
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="still")
        self._closed = False
        self._report = report
        self._note = note
        # Folders already reported for refusing a still. One that is read-only
        # refuses every still it is asked for, and a library holds hundreds:
        # reported once while this maker lives, noted after that. Main thread only.
        self._refused_reported: set[Path] = set()
        self._pending: set[Future[None]] = set()
        self._lock = Lock()
        self._processes = worker_processes.Cancellation()
        # Videos already attempted, whether or not it worked. Without this a
        # rescan after a successful batch would queue the failures again, and
        # since a finished batch *causes* a rescan, a video ffmpeg cannot read
        # would loop forever.
        self._attempted: set[Path] = set()

    def request(self, items: Iterable[MediaItem], root: Path, callback: Callback) -> None:
        """Make stills for anything in ``items`` that moves and has none.

        Returns immediately. ``callback`` runs on the main thread once, and
        only if something was actually made -- a rescan that would change
        nothing is not worth the disk.
        """
        if self._closed:
            return
        web = self._web_allowed()
        wanted = [
            item
            for item in items
            if not workshop.previews_off(item, web)
            # A web page or a preset gets an automatic still only from its own
            # preview, and only with that setting on (`workshop.no_automatic_still`).
            and not workshop.no_automatic_still(item, self._previews())
            # Every moving kind, not video alone. `stills.ensure` has always
            # known how to capture a Wallpaper Engine scene through the engine
            # itself, and this filter was the only reason it was never asked
            # to. Under the pairing model every item is a pairing, and a scene
            # with no still has nothing to show when dynamics are off and
            # nothing for an adaptive palette to be generated from.
            #
            # A scene whose still is the managed one is handed over too, so
            # the worker can see whether it is an old undersized capture. That
            # needs the still's header and the service's answer, and this runs
            # on the main thread at the end of every scan: only path
            # arithmetic here.
            if item.kind.moves
            # Listed but never played (`MediaItem.unplayable`): no still to make.
            and item.unplayable is None
            and (
                item.paired_still is None
                or (
                    item.kind is Kind.SCENE
                    and item.paired_still == stills.automatic_destination(item, root)
                )
            )
            and item.path not in self._attempted
        ]
        if not wanted:
            return
        self._attempted.update(item.path for item in wanted)
        # Videos first. A scene capture spawns the engine and waits for it to
        # render and for the file to settle -- seconds each, against a
        # fraction of one for an ffmpeg seek -- and the pool is one worker
        # wide, so leaving the two interleaved would let a handful of scenes
        # hold up every video behind them.
        wanted.sort(key=lambda item: item.kind is Kind.SCENE)
        # GTK's monitors can only be read here, on the main thread. A still
        # made from a preview needs no display.
        fallback = (
            self._local_displays()
            if any(_engine_capture(item, self._previews()) for item in wanted)
            else ()
        )
        self._submit(
            self._run,
            tuple(wanted),
            root,
            callback,
            fallback,
            context="Automatic still generation stopped unexpectedly",
        )

    def _local_displays(self) -> tuple[outputs.Output, ...]:
        """GTK's view of the screens. Main thread only."""
        return self._displays() if self._displays is not None else monitors.snapshot()

    def _submit(
        self,
        work: Callable[..., None],
        *arguments: object,
        context: str,
    ) -> None:
        """Submit and observe every worker result, including cancellations."""
        with self._lock:
            if self._closed:
                return
            try:
                future = self._pool.submit(work, *arguments)
            except RuntimeError:
                # ``shutdown`` won the executor race after the caller's
                # initial closed check.  That is a normal rejected late job.
                return
            self._pending.add(future)
        future.add_done_callback(lambda done: self._finished(done, context))

    def _finished(self, future: Future[None], context: str) -> None:
        with self._lock:
            self._pending.discard(future)
            closed = self._closed
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as error:  # defensive boundary around ffmpeg/scene helpers
            if closed:
                # Retrieving the exception is still important; cancellation
                # failures are expected once the owner has gone away and have
                # nowhere useful to be delivered.
                return
            message = f"{context}: {error}"

            def deliver() -> bool:
                if not self._closed:
                    if self._report is not None:
                        self._report(message)
                    else:
                        LOGGER.warning("%s", message)
                return GLib.SOURCE_REMOVE

            # Plain idle (200) sits below GTK's redraw (120) and starves while tiles spin.
            GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _run(
        self,
        items: tuple[MediaItem, ...],
        root: Path,
        callback: Callback,
        fallback: tuple[outputs.Output, ...] = (),
    ) -> None:
        made = 0
        # The folders that would not be written in, with how many stills each
        # refused and the first reason given (`stills.StillFolderError`).
        refused: dict[Path, tuple[int, str]] = {}

        def folder_refused(error: stills.StillFolderError) -> None:
            count, reason = refused.get(error.folder, (0, error.reason))
            refused[error.folder] = (count + 1, reason)

        # The scenes the engine ran for and gave no still, with why (`_attempted`
        # keeps each from being asked again while this maker lives).
        failures: list[tuple[str, str]] = []

        def capture_failed(name: str) -> Callable[[stills.CaptureFailedError], None]:
            return lambda error: failures.append((name, str(error)))

        # Asked once per batch, here on the worker: the service's answer is a
        # socket round trip, and neither the main thread nor every scene
        # should wait on one. `fallback` is GTK's view, for when no service
        # answers. With no display measured no scene still can be taken, and
        # an existing one is never judged against a guess.
        display = (
            scenes.measured_capture_size("", outputs.discover(fallback))
            if any(_engine_capture(i, self._previews()) for i in items)
            else None
        )
        for item in items:
            if self._processes.cancelled():
                return
            web = self._web_allowed()
            if workshop.previews_off(item, web):
                # Turned off since this was queued: nothing of it is opened,
                # and it is not counted as attempted, so turning web on makes it.
                self._attempted.discard(item.path)
                continue
            if workshop.no_automatic_still(item, self._previews()):
                # The preview setting went off since this was queued: nothing is
                # made, and it is not counted as attempted, so turning it back
                # on makes it.
                self._attempted.discard(item.path)
                continue
            processes = (
                worker_processes.GatedCancellation(self._processes, self._web_allowed)
                if item.holds_web
                else self._processes
            )
            if (
                item.kind is Kind.SCENE
                and display is None
                and _engine_capture(item, self._previews())
            ):
                # No display can be measured now, so no engine capture: not counted as
                # attempted, so a later request (the next scan, a display that has
                # appeared) asks again instead of never in this process.
                self._attempted.discard(item.path)
                continue
            if (
                item.kind is Kind.SCENE
                and item.paired_still is not None
                and not stills.scene_capture_required(
                    item, root, size=display, automatic=True, previews=self._previews()
                )
            ):
                # Kept as it is. Not counted: a rescan would change nothing.
                continue
            # `ensure` swallows its own failures: a still that cannot be made
            # is not a reason to stop making the others, and the video still
            # plays either way. The one it hands over is a folder that would
            # not be written in, which is nothing ffmpeg or the video did.
            if (
                stills.ensure(
                    item,
                    root,
                    processes=processes,
                    size=display,
                    refused=folder_refused,
                    # Only a web page is told: for it alone is it ever true, and
                    # `stills.ensure` has the safe default.
                    previews=self._previews(),
                    failed=capture_failed(item.name),
                    **({"allow_web": True} if item.holds_web else {}),
                )
                is not None
            ):
                made += 1
            elif self._withdrawn_if_cancelled(item, processes):
                continue
            elif not item.holds_web and workshop.web_files_now(item):
                # Refused because its folder may be a web wallpaper's now, even
                # with web on: only an item the scan knew as one is told the
                # setting. Not counted as attempted, so the rescan's is asked for.
                self._attempted.discard(item.path)
        if self._processes.cancelled():
            return
        if refused:
            # Whether or not anything was made: a batch can do both. It asks
            # for no rescan, and `_attempted` keeps these from being queued
            # again, so a folder that refuses is asked once for each wallpaper.
            # Plain idle (200) sits below GTK's redraw (120) and starves while tiles spin.
            GLib.idle_add(  # type: ignore[call-arg]
                self._say_refused, refused, priority=GLib.PRIORITY_DEFAULT
            )
        if failures:
            # Said once for the batch: each is asked once while this maker lives.
            GLib.idle_add(  # type: ignore[call-arg]
                self._say_failed, failures, priority=GLib.PRIORITY_DEFAULT
            )
        if made == 0:
            return

        def deliver() -> bool:
            if not self._processes.cancelled():
                callback(made)
            return GLib.SOURCE_REMOVE

        # Plain idle (200) sits below GTK's redraw (120) and starves while tiles spin.
        GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _withdrawn_if_cancelled(
        self, item: MediaItem, processes: worker_processes.Cancellation
    ) -> bool:
        """Whether ``item``'s capture was cancelled: web turned off for a page, or closing.

        That is no failure to say, and it is not counted as attempted, so turning
        web on makes it. Asked once a capture gave no still, automatic or asked for.
        """
        if not processes.cancelled():
            return False
        self._attempted.discard(item.path)
        return True

    def _say_refused(self, refused: dict[Path, tuple[int, str]]) -> bool:
        """Report each folder that refused, the first time; note it every time after."""
        if self._closed:
            return GLib.SOURCE_REMOVE
        for folder, (count, reason) in refused.items():
            say = self._note if folder in self._refused_reported else self._report
            self._refused_reported.add(folder)
            message = _refusal(folder, count, reason)
            if say is not None:
                say(message)
            else:
                LOGGER.warning("%s", message)
        return GLib.SOURCE_REMOVE

    def _say_failed(self, failures: list[tuple[str, str]]) -> bool:
        """Report the scenes the engine gave no still for (`_captures_failed`)."""
        if self._closed:
            return GLib.SOURCE_REMOVE
        message = _captures_failed(failures)
        if self._report is not None:
            self._report(message)
        else:
            LOGGER.warning("%s", message)
        return GLib.SOURCE_REMOVE

    def regenerate_scene(self, item: MediaItem, root: Path, callback: Callback) -> None:
        """Force one managed scene still to be replaced, off the UI thread."""
        if (
            self._closed
            or item.kind is not Kind.SCENE
            or workshop.no_automatic_still(item, self._previews())
            or workshop.previews_off(item, self._web_allowed())
        ):
            return
        self._attempted.add(item.path)
        # GTK's monitors can only be read here, on the main thread.
        fallback = self._local_displays()

        def run() -> None:
            web = self._web_allowed()
            if workshop.previews_off(item, web):
                self._attempted.discard(item.path)
                return
            processes = (
                worker_processes.GatedCancellation(self._processes, self._web_allowed)
                if item.holds_web
                else self._processes
            )
            size = scenes.measured_capture_size("", outputs.discover(fallback))
            try:
                stills.capture_scene(
                    item,
                    root,
                    force=True,
                    processes=processes,
                    size=size,
                    previews=self._previews(),
                    **({"allow_web": True} if item.holds_web else {}),
                )
            except stills.StillError:
                if self._withdrawn_if_cancelled(item, processes):
                    return  # as `_run`'s (R42-13)
                raise
            if not self._processes.cancelled():

                def deliver() -> bool:
                    callback(1)
                    return GLib.SOURCE_REMOVE

                # Plain idle (200) sits below redraw (120) and starves while tiles spin.
                GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

        self._submit(
            run,
            context=f"Could not regenerate the still for {item.name}",
        )

    def forget(self, path: Path) -> None:
        """Allow ``path`` to be attempted again.

        For a video whose file changed underneath us. Nothing calls this yet;
        it exists so that the memo above is a cache rather than a one-way door.
        """
        self._attempted.discard(path)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
            self._pending.clear()
        self._processes.cancel()
        for future in pending:
            future.cancel()
        # Not waiting: a 4K frame grab takes about a second and quitting should
        # be immediate. `library.stills` writes to a temporary name and renames,
        # so a still interrupted here leaves nothing half-written to be found.
        self._pool.shutdown(wait=False, cancel_futures=True)
