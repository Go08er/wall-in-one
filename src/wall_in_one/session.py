"""The running state of the manager: what is in the library and what is on screen.

Deliberately free of GTK. Everything here is callable from a test with no
display and no compositor; the UI layer adds a window and a timer on top and
nothing else. That split is what lets the control verbs -- which is the entire
surface the Noctalia plugin drives -- be tested directly.
"""

from __future__ import annotations

import contextlib
import os
import random
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from wall_in_one import config, file_io
from wall_in_one.library import (
    displays,
    favourites,
    manage,
    pairings,
    playlists,
    removals,
    scan,
    schedules,
    stills,
    workshop,
)
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.library.pairings import Store as PairingStore
from wall_in_one.library.playlist import Playlist
from wall_in_one.library.playlists import Playlist as NamedPlaylist
from wall_in_one.theme import noctalia
from wall_in_one.wallpaper import renderer, scenes
from wall_in_one.wallpaper.applier import Applied, Applier, ApplyError

Scanner = Callable[[Sequence[Path] | None], Library]


@dataclass(frozen=True, slots=True)
class LibraryScan:
    """One immutable library-scan request, safe to execute off the UI thread.

    Pairing records and Workshop roots are captured before the worker starts.
    That matters when a setting or pairing changes while a slow filesystem is
    still being walked: the application can tag this request as stale and run
    a newer snapshot, rather than letting one scan mix old and new state.

    An injected scanner is retained for headless callers and tests.  The real
    scanner receives every input explicitly, including Steam roots, so a
    worker never reaches out to mutable ``Session`` state or re-resolves HOME.
    """

    roots: tuple[Path, ...] | None
    records: dict[str, pairings.Pairing]
    include_workshop: bool
    workshop_roots: tuple[Path, ...]
    scanner: Scanner | None = None
    cancelled: Callable[[], bool] | None = None

    def run(self) -> Library:
        if self.cancelled is not None and self.cancelled():
            raise scan.ScanCancelledError
        if self.scanner is not None:
            library = self.scanner(self.roots)
            if self.cancelled is not None and self.cancelled():
                raise scan.ScanCancelledError
            return library
        return scan.scan(
            self.roots,
            self.records,
            include_workshop=self.include_workshop,
            workshop_roots=self.workshop_roots,
            cancelled=self.cancelled,
        )


@dataclass(frozen=True, slots=True)
class _WorkshopCleanup:
    """A confirmed uninstall whose durable authoring cleanup must finish."""

    item: MediaItem
    roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class RemovalCleanup:
    """Exact in-memory delta produced by one durable worker transaction.

    Each success bit means the detached worker Store completed its locked,
    reread-and-rebased mutation.  GTK may then mirror only that semantic
    deletion into its live Store; it must never replace the Store wholesale
    with a snapshot which could predate an interactive edit.
    """

    item: MediaItem
    removed_stills: tuple[Path, ...]
    favourite_succeeded: bool
    pairing_succeeded: bool
    playlist_succeeded: bool


@dataclass(frozen=True, slots=True)
class LibraryRefreshResult:
    """A finished worker reconciliation containing no live Session state."""

    library: Library
    replay_failures: tuple[str, ...]
    cleanups: tuple[RemovalCleanup, ...]
    removed_workshop: tuple[MediaItem, ...]
    workshop_cleanup_failures: tuple[str, ...]
    known_workshop: tuple[tuple[str, MediaItem], ...]
    pending_workshop_cleanup: tuple[tuple[str, _WorkshopCleanup], ...]
    repaired_faults: tuple[tuple[str, str], ...]


class LibraryRefreshCancelledError(Exception):
    """A superseded refresh, retaining any cleanup already committed."""

    def __init__(self, cleanups: Sequence[RemovalCleanup] = ()) -> None:
        super().__init__("library refresh was superseded")
        self.cleanups = tuple(cleanups)


class LibraryRefreshBusyError(Exception):
    """A live Delete/Trash transaction still owns the removal journal."""


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """A complete detached Delete/Trash transaction returned to GTK.

    ``committed`` is the only authority for removing the item from the visible
    library.  It remains true when the physical operation crossed its commit
    point but a later fsync or metadata cleanup failed.  Store snapshots never
    cross back to GTK; the narrow cleanup deltas are safe to adopt over newer
    interactive edits.
    """

    item: MediaItem
    trash: bool
    committed: bool
    physical: manage.Removal | manage.Trashed | None = None
    error_kind: str = ""
    error_message: str = ""
    cleanup_failures: tuple[str, ...] = ()
    cancellation_failures: tuple[str, ...] = ()
    cleanups: tuple[RemovalCleanup, ...] = ()
    repaired_faults: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    """Value-only removal inputs whose lease and I/O live on one worker.

    The detached removal Store owns the operation flock from durable intent
    through the physical mutation and every metadata tail.  Closing the live
    Session during application shutdown therefore cannot release another
    thread's lease halfway through an unlink or trash move.
    """

    settings: config.Settings
    item: MediaItem
    roots: tuple[Path, ...]
    trash: bool
    favourite_store: favourites.Store
    pairing_store: pairings.Store
    playlist_store: playlists.Store
    removal_store: removals.Store

    def run(self) -> RemovalResult:
        """Perform prepare, physical commit and cleanup on this worker."""
        favourite_store = self.favourite_store.worker_copy(rebase=True)
        pairing_store = self.pairing_store.worker_copy(rebase=True)
        playlist_store = self.playlist_store.worker_copy(rebase=True)
        removal_store = self.removal_store.worker_copy(rebase=True)
        stores = (
            ("favourites", self.favourite_store, favourite_store),
            ("pairings", self.pairing_store, pairing_store),
            ("playlists", self.playlist_store, playlist_store),
            ("pending removals", self.removal_store, removal_store),
        )
        repaired_faults = tuple(
            (name, original.fault)
            for name, original, rebased in stores
            if original.fault is not None and rebased.fault is None
        )
        snapshot = Session(
            self.settings,
            favourite_store=favourite_store,
            pairing_store=pairing_store,
            playlist_store=playlist_store,
            removal_store=removal_store,
            schedule_store=schedules.Store(),
            display_store=displays.Store(),
        )
        cleanups: list[RemovalCleanup] = []
        snapshot._cleanup_observer = cleanups.append
        source_context: file_io.PinnedDirectoryContext | None = None
        try:
            try:
                intent = snapshot.prepare_removal(self.item, self.roots)
            except removals.RemovalJournalError as error:
                return RemovalResult(
                    item=self.item,
                    trash=self.trash,
                    committed=False,
                    error_kind=error.kind,
                    error_message=str(error),
                    repaired_faults=repaired_faults,
                )

            try:
                source_context = intent.pin_source_context()
            except (OSError, ValueError) as error:
                context_cancellation_failures: tuple[str, ...] = ()
                try:
                    snapshot.cancel_removal(intent)
                except removals.RemovalJournalError as cancellation:
                    context_cancellation_failures = (f"removal journal: {cancellation}",)
                return RemovalResult(
                    item=self.item,
                    trash=self.trash,
                    committed=False,
                    error_kind="local-io",
                    error_message=(
                        f"could not retain the prepared source directory for {self.item.path}: "
                        f"{error}"
                    ),
                    cancellation_failures=context_cancellation_failures,
                    repaired_faults=repaired_faults,
                )

            try:
                source_pin = snapshot.removal_journal.source_pin(intent)
            except removals.RemovalJournalError as error:
                source_context.close()
                pin_cancellation_failures: tuple[str, ...] = ()
                try:
                    snapshot.cancel_removal(intent)
                except removals.RemovalJournalError as cancellation:
                    pin_cancellation_failures = (f"removal journal: {cancellation}",)
                return RemovalResult(
                    item=self.item,
                    trash=self.trash,
                    committed=False,
                    error_kind=error.kind,
                    error_message=str(error),
                    cancellation_failures=pin_cancellation_failures,
                    repaired_faults=repaired_faults,
                )

            try:
                physical: manage.Removal | manage.Trashed
                anchored_source = source_context.child(intent.path.name)
                if self.trash:
                    physical = manage.trash(
                        self.item,
                        self.roots,
                        expected_source=intent.source_identity,
                        expected_fingerprint=intent.source_fingerprint,
                        prepared_pin=source_pin,
                        lookup_path=anchored_source,
                        source_root=intent.source_root,
                        lookup_root=source_context.root_anchor,
                        lookup_parent=source_context.directory_anchor,
                        source_context=source_context,
                    )
                else:
                    physical = manage.remove(
                        self.item,
                        self.roots,
                        expected_source=intent.source_identity,
                        expected_fingerprint=intent.source_fingerprint,
                        prepared_pin=source_pin,
                        operation_token=intent.token,
                        lookup_path=anchored_source,
                        source_root=intent.source_root,
                        lookup_root=source_context.root_anchor,
                        lookup_parent=source_context.directory_anchor,
                        source_context=source_context,
                    )
            except manage.ManageError as error:
                if not error.committed:
                    if error.retain_intent:
                        snapshot.retain_removal(intent)
                        return RemovalResult(
                            item=self.item,
                            trash=self.trash,
                            committed=False,
                            error_kind=error.kind,
                            error_message=str(error),
                            repaired_faults=repaired_faults,
                        )
                    cancellation_failures: tuple[str, ...] = ()
                    try:
                        snapshot.cancel_removal(intent)
                    except removals.RemovalJournalError as cancellation:
                        cancellation_failures = (f"removal journal: {cancellation}",)
                    return RemovalResult(
                        item=self.item,
                        trash=self.trash,
                        committed=False,
                        error_kind=error.kind,
                        error_message=str(error),
                        cancellation_failures=cancellation_failures,
                        repaired_faults=repaired_faults,
                    )
                cleanup_failures = snapshot.commit_removal(
                    intent,
                    artifacts_already_clean=True,
                    artifact_source_root=intent.source_root,
                    artifact_lookup_root=source_context.root_anchor,
                    artifact_lookup_parent=source_context.directory_anchor,
                    artifact_source_context=source_context,
                )
                return RemovalResult(
                    item=self.item,
                    trash=self.trash,
                    committed=True,
                    physical=error.physical,
                    error_kind=error.kind,
                    error_message=str(error),
                    cleanup_failures=cleanup_failures,
                    cleanups=tuple(cleanups),
                    repaired_faults=repaired_faults,
                )

            cleanup_failures = snapshot.commit_removal(
                intent,
                # The physical phase already attempted every companion it
                # pinned before the media commit. Re-discovery here could
                # mistake a newly installed same-path lifecycle for leftovers.
                artifacts_already_clean=True,
                artifact_source_root=intent.source_root,
                artifact_lookup_root=source_context.root_anchor,
                artifact_lookup_parent=source_context.directory_anchor,
                artifact_source_context=source_context,
            )
            return RemovalResult(
                item=self.item,
                trash=self.trash,
                committed=True,
                physical=physical,
                cleanup_failures=cleanup_failures,
                cleanups=tuple(cleanups),
                repaired_faults=repaired_faults,
            )
        finally:
            try:
                if source_context is not None:
                    source_context.close()
            finally:
                snapshot.shutdown()


@dataclass(frozen=True, slots=True)
class LibraryRefreshPlan:
    """Detached inputs for scan, recovery and Workshop reconciliation.

    The four Stores are private copies.  :meth:`run` first rebases each copy
    from its durable file, so all cleanup mutations use their normal locking
    transaction and cannot overwrite a concurrent process.  Only explicit
    cleanup deltas and immutable scan state return to the live Session.
    """

    settings: config.Settings
    scanner: Scanner | None
    favourite_store: favourites.Store
    pairing_store: pairings.Store
    playlist_store: playlists.Store
    removal_store: removals.Store
    known_workshop: tuple[tuple[str, MediaItem], ...]
    pending_workshop_cleanup: tuple[tuple[str, _WorkshopCleanup], ...]
    cancelled: threading.Event = field(
        default_factory=threading.Event,
        compare=False,
        repr=False,
    )

    def cancel(self) -> None:
        """Request cooperative cancellation between bounded I/O phases."""
        self.cancelled.set()

    def _check_cancelled(self, cleanups: Sequence[RemovalCleanup] = ()) -> None:
        if self.cancelled.is_set():
            raise LibraryRefreshCancelledError(cleanups)

    def scan(self) -> Library:
        """Walk media without mutating lifecycle metadata.

        Graphical callers run this phase on the scan pool, then serialize
        :meth:`reconcile` through the authoring actor.  A confirmed external
        uninstall must not clean Stores concurrently with a queued favourite,
        pairing, playlist, or runtime-health write.
        """
        self._check_cancelled()
        pairing_store = self.pairing_store.worker_copy(rebase=True)
        snapshot = Session(
            self.settings,
            scanner=self.scanner,
            pairing_store=pairing_store,
            favourite_store=favourites.Store(),
            playlist_store=playlists.Store(),
            removal_store=removals.Store(),
            schedule_store=schedules.Store(),
            display_store=displays.Store(),
        )
        try:
            request = snapshot.prepare_scan(cancelled=self.cancelled.is_set)
            try:
                library = request.run()
            except scan.ScanCancelledError as error:
                raise LibraryRefreshCancelledError from error
            self._check_cancelled()
            return library
        finally:
            snapshot.shutdown()

    def reconcile(self, library: Library) -> LibraryRefreshResult:
        """Replay and clean lifecycle metadata in the total authoring order."""
        self._check_cancelled()
        cleanups: list[RemovalCleanup] = []
        favourite_store = self.favourite_store.worker_copy(rebase=True)
        pairing_store = self.pairing_store.worker_copy(rebase=True)
        playlist_store = self.playlist_store.worker_copy(rebase=True)
        removal_store = self.removal_store.worker_copy(rebase=True)
        stores = (
            ("favourites", self.favourite_store, favourite_store),
            ("pairings", self.pairing_store, pairing_store),
            ("playlists", self.playlist_store, playlist_store),
            ("pending removals", self.removal_store, removal_store),
        )
        repaired_faults = tuple(
            (name, original.fault)
            for name, original, rebased in stores
            if original.fault is not None and rebased.fault is None
        )
        snapshot = Session(
            self.settings,
            scanner=self.scanner,
            favourite_store=favourite_store,
            pairing_store=pairing_store,
            playlist_store=playlist_store,
            removal_store=removal_store,
            schedule_store=schedules.Store(),
            display_store=displays.Store(),
        )
        snapshot._known_workshop = dict(self.known_workshop)
        snapshot._pending_workshop_cleanup = dict(self.pending_workshop_cleanup)
        snapshot._cleanup_observer = cleanups.append
        try:
            replay_failures = snapshot.retry_removals()
            self._check_cancelled(cleanups)
            snapshot.adopt_library(library)
            self._check_cancelled(cleanups)
            return LibraryRefreshResult(
                library=snapshot.library,
                replay_failures=replay_failures,
                cleanups=tuple(cleanups),
                removed_workshop=snapshot.removed_workshop,
                workshop_cleanup_failures=snapshot.workshop_cleanup_failures,
                known_workshop=tuple(sorted(snapshot._known_workshop.items())),
                pending_workshop_cleanup=tuple(sorted(snapshot._pending_workshop_cleanup.items())),
                repaired_faults=repaired_faults,
            )
        finally:
            snapshot.shutdown()

    def run(self) -> LibraryRefreshResult:
        """Keep the synchronous compatibility contract as the two safe phases."""
        return self.reconcile(self.scan())


#: The one-entry playlist created when Media is activated.  A fixed id means
#: repeated choices replace one visible playlist instead of filling the store
#: with disposable records.
QUICK_CHOICE_ID = "quick-choice"
QUICK_CHOICE_NAME = "Quick choice"


def _renderer_for(settings: config.Settings) -> renderer.Renderer:
    """A video renderer carrying the user's playback settings from the start.

    Set at construction rather than pushed afterwards, because `when_hidden`
    becomes a command-line flag of mpvpaper's and cannot be changed once a
    video is playing.
    """
    return renderer.Renderer(
        output=settings.output or renderer.ALL_OUTPUTS,
        when_hidden=settings.video_when_hidden,
        hardware_decode=settings.video_hardware_decode,
        interpolation=settings.video_interpolation,
        muted=settings.video_muted,
        volume=settings.video_volume,
    )


class Session:
    """Library, play order, and the wallpaper currently applied."""

    def __init__(
        self,
        settings: config.Settings,
        *,
        applier: Applier | None = None,
        scanner: Scanner | None = None,
        rng: random.Random | None = None,
        favourite_store: favourites.Store | None = None,
        pairing_store: pairings.Store | None = None,
        playlist_store: playlists.Store | None = None,
        schedule_store: schedules.Store | None = None,
        display_store: displays.Store | None = None,
        removal_store: removals.Store | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        # Only when we build the renderer ourselves: an applier handed in has
        # been configured by whoever handed it in, and reaching into it would
        # overwrite that.
        self._applier = (
            applier
            if applier is not None
            else Applier(
                _renderer_for(settings),
                settings.output,
                scenes.SceneRenderer(
                    output=settings.output,
                    fps=settings.scene_fps,
                    scaling=settings.scene_scaling,
                    clamp=settings.scene_clamp,
                ),
                own_scene_renderer=settings.own_scene_renderer,
            )
        )
        # The default scanner carries the customizations in with it, so pairing
        # happens once, inside the scan. An injected scanner is left alone: a
        # test that hands over a ready-made library means it, and re-resolving
        # would recompute every pairing from a disk the test never wrote to.
        self._scanner = scanner
        self._library = Library(roots=(), items=())
        # Keep successfully observed Workshop installations across malformed
        # or missing-entry scans. The ordinary Library snapshot intentionally
        # drops unplayable items, but that must not erase the evidence needed
        # to recognize a later, confirmed uninstall in this process.
        self._known_workshop: dict[str, MediaItem] = {}
        self._removed_workshop: tuple[MediaItem, ...] = ()
        self._pending_workshop_cleanup: dict[str, _WorkshopCleanup] = {}
        self._workshop_cleanup_failures: tuple[str, ...] = ()
        self._cleanup_observer: Callable[[RemovalCleanup], None] | None = None
        #: What the schedule last asked for, so a tick can tell whether the
        #: calendar has moved without rebuilding to find out.
        self._in_force = ""
        #: An on-demand playlist selection temporarily sits above the
        #: calendar.  It is intentionally runtime-only: after a service
        #: restart the saved schedule is authoritative again.
        self._manual_playlist: str | None = None
        self._rng = rng if rng is not None else random.Random()
        self._playlist = Playlist(shuffle=settings.shuffle, rng=self._rng)
        # Owned here rather than by the window, because the rotation is built
        # from them and the window is not allowed to be the only thing that
        # knows. `open` never raises: an unreadable list degrades to none.
        self._favourites = (
            favourite_store if favourite_store is not None else favourites.Store.open()
        )
        # Owned here for the same reason the favourites are: the rotation and
        # the applier are built from what it resolves, so the window cannot be
        # the only thing that knows.
        self._pairings = pairing_store if pairing_store is not None else pairings.Store.open()
        self._playlists = playlist_store if playlist_store is not None else playlists.Store.open()
        self._removals = removal_store if removal_store is not None else removals.Store.open()
        self._schedules = schedule_store if schedule_store is not None else schedules.Store.open()
        self._displays = display_store if display_store is not None else displays.Store.open()
        # Injected so a schedule can be tested at three in the morning in
        # December without waiting until then.
        self._now: Callable[[], datetime] = clock if clock is not None else datetime.now

    # -- state -----------------------------------------------------------

    @property
    def settings(self) -> config.Settings:
        return self._settings

    @property
    def library(self) -> Library:
        return self._library

    @property
    def playlist(self) -> Playlist:
        return self._playlist

    @property
    def current(self) -> Applied | None:
        """What *this app* last applied, or ``None`` if it has not applied yet."""
        return self._applier.current

    @property
    def cursor(self) -> MediaItem | None:
        """The wallpaper the Python compatibility driver is pointing at.

        Not the same as :attr:`current`. The Media grid uses this only when no
        valid Rust status snapshot exists; normally the runtime's per-display
        entry ids are the playback truth. In compatibility mode,
        `sync_with_noctalia` and the local navigation actions move it.
        """
        return self._playlist.current()

    # -- library ---------------------------------------------------------

    def refresh(
        self,
        roots: Sequence[Path] | None = None,
        *,
        mutate_removals: bool = True,
    ) -> Library:
        """Rescan and rebuild the play order, keeping our place if we can.

        With no roots given, the configured ones are used. An empty configured
        list is deliberately passed through as empty: first run must ask the
        person where writes belong instead of silently adopting Noctalia's
        wallpaper directory. Resolving it here rather than at each call site
        makes startup, refresh and completed-download scans agree.
        """
        if mutate_removals:
            self.retry_removals()
        return self.adopt_library(
            self.prepare_scan(roots).run(),
            reconcile_workshop=mutate_removals,
        )

    def prepare_scan(
        self,
        roots: Sequence[Path] | None = None,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> LibraryScan:
        """Snapshot all inputs for a scan without walking the library.

        GUI code calls this on GTK's thread and runs :meth:`LibraryScan.run`
        elsewhere.  Keeping the snapshot here means the synchronous and async
        paths cannot quietly disagree about configured roots or pairings.
        """
        if roots is None:
            roots = self._settings.roots
        resolved_roots = tuple(roots)
        # Scene/video stills need an app-owned destination too. Until the
        # first root is chosen, do not quietly make the real Steam tree the
        # apparent library and then fail every operation which needs a still.
        include_workshop = self._settings.scan_workshop and bool(self._settings.roots)
        return LibraryScan(
            roots=resolved_roots,
            records=dict(self._pairings.records),
            include_workshop=include_workshop,
            workshop_roots=workshop.steam_roots() if include_workshop else (),
            scanner=self._scanner,
            cancelled=cancelled,
        )

    def prepare_library_refresh(self) -> LibraryRefreshPlan:
        """Snapshot one graphical refresh without performing filesystem I/O.

        A Delete/Trash operation owns an uncopyable process-local lease until
        its physical mutation and journal commit finish.  Refuse a concurrent
        refresh briefly; the removal completion path immediately asks again.
        """
        if self._removals.operation_owned:
            raise LibraryRefreshBusyError("a wallpaper removal is still in progress")
        return LibraryRefreshPlan(
            settings=self._settings,
            scanner=self._scanner,
            favourite_store=self._favourites.worker_copy(),
            pairing_store=self._pairings.worker_copy(),
            playlist_store=self._playlists.worker_copy(),
            removal_store=self._removals.worker_copy(),
            known_workshop=tuple(sorted(self._known_workshop.items())),
            pending_workshop_cleanup=tuple(sorted(self._pending_workshop_cleanup.items())),
        )

    def adopt_library_refresh(self, result: LibraryRefreshResult) -> Library:
        """Install an accepted worker result using semantic cleanup deltas.

        No worker Store is installed here.  Interactive mutations which
        occurred during a slow scan therefore remain intact; only metadata
        whose durable deletion actually succeeded is removed in memory.
        """
        self.adopt_library_cleanups(result.cleanups)
        self.adopt_library_fault_repairs(result.repaired_faults)
        self._known_workshop = dict(result.known_workshop)
        self._pending_workshop_cleanup = dict(result.pending_workshop_cleanup)
        adopted = self.adopt_library(result.library, reconcile_workshop=False)
        # ``adopt_library(..., reconcile_workshop=False)`` deliberately resets
        # this transient value. Restore the worker's accepted observation only
        # after that pure Library/cursor adoption has completed.
        self._removed_workshop = result.removed_workshop
        self._workshop_cleanup_failures = result.workshop_cleanup_failures
        return adopted

    def adopt_library_fault_repairs(self, repairs: Sequence[tuple[str, str]]) -> None:
        """Clear an unchanged live fault which a worker proved was repaired."""
        for name, expected in repairs:
            if name == "favourites":
                self._favourites.adopt_worker_repair(expected)
            elif name == "pairings":
                self._pairings.adopt_worker_repair(expected)
            elif name == "playlists":
                self._playlists.adopt_worker_repair(expected)
            elif name == "pending removals":
                self._removals.adopt_worker_repair(expected)

    def adopt_library_cleanups(self, cleanups: Sequence[RemovalCleanup]) -> None:
        """Mirror durable cleanup deltas even when their scan was superseded.

        Cleanup transactions are lifecycle facts independent of a scan's
        roots: once a deletion journal committed, a newer library generation
        must not resurrect its in-process metadata.  Every operation below is
        narrowly semantic, so unrelated interactive edits remain untouched.
        """
        for cleanup in cleanups:
            if cleanup.favourite_succeeded:
                self._favourites.adopt_worker_discard(cleanup.item.path)
            if cleanup.pairing_succeeded:
                self._pairings.adopt_worker_forget_item(
                    cleanup.item,
                    removed_stills=cleanup.removed_stills,
                )
            if cleanup.playlist_succeeded:
                self._playlists.adopt_worker_forget_path(cleanup.item.path)

    def adopt_library(
        self,
        library: Library,
        *,
        reconcile_workshop: bool = True,
    ) -> Library:
        """Install one completed scan and reconcile the active play order.

        This is deliberately separate from the filesystem work so GUI callers
        can guarantee mutation happens only on GTK's main thread.
        """
        self._removed_workshop = (
            self._forget_confirmed_workshop_removals(library) if reconcile_workshop else ()
        )
        self._library = library
        self._rebuild_playlist()
        return self._library

    @property
    def removed_workshop(self) -> tuple[MediaItem, ...]:
        """Workshop installs conclusively removed by the most recent scan."""
        return self._removed_workshop

    @property
    def workshop_cleanup_failures(self) -> tuple[str, ...]:
        """Actionable store/artifact failures still pending after reconciliation."""
        return self._workshop_cleanup_failures

    @property
    def removal_journal(self) -> removals.Store:
        """Explicit removal intents, retained until cleanup is complete."""
        return self._removals

    def prepare_removal(
        self,
        item: MediaItem,
        roots: Sequence[Path],
    ) -> removals.Intent:
        """Durably authorize a local removal before media can be touched."""
        return self._removals.prepare(item, roots)

    def prepare_removal_plan(self, item: MediaItem, *, trash: bool) -> RemovalPlan:
        """Snapshot a detached removal without doing filesystem I/O on GTK."""
        roots = tuple(self._library.roots)
        return RemovalPlan(
            settings=self._settings,
            item=item,
            roots=roots,
            trash=trash,
            favourite_store=self._favourites.worker_copy(),
            pairing_store=self._pairings.worker_copy(),
            playlist_store=self._playlists.worker_copy(),
            removal_store=self._removals.worker_copy(),
        )

    def cancel_removal(self, intent: removals.Intent) -> None:
        """Cancel an intent whose physical operation did not commit."""
        self._removals.discard(intent)

    def retain_removal(self, intent: removals.Intent) -> None:
        """Release a failed operation while preserving its uncommitted intent.

        A no-replace restore conflict can leave an unverified entry in the
        token claim. The media deletion did not commit, so metadata cleanup
        must not start, but discarding the journal would orphan the only
        durable authority which makes that private location discoverable.
        """
        self._removals.finish_operation(intent)

    def commit_removal(
        self,
        intent: removals.Intent,
        *,
        artifacts_already_clean: bool = False,
        artifact_cleanup_deferred: bool = False,
        artifact_source_root: Path | None = None,
        artifact_lookup_root: Path | None = None,
        artifact_lookup_parent: Path | None = None,
        artifact_source_context: file_io.PinnedDirectoryContext | None = None,
    ) -> tuple[str, ...]:
        """Finish a committed removal and clear its journal only when clean."""
        if (
            not artifacts_already_clean
            and not artifact_cleanup_deferred
            and artifact_source_context is None
        ):
            # A pathname alone is not deletion authority after the media
            # commit: a remount or rename could redirect artifact cleanup into
            # a replacement library. Keep the journal actionable until the
            # prepared directory context can be repinned.
            artifact_cleanup_deferred = True
        journal_failure: str | None = None
        try:
            try:
                intent = self._removals.mark_committed(intent)
            except removals.RemovalJournalError as error:
                # Only a post-delete I/O failure with the exact token-owned
                # prepared record still durable may continue. Invalid state or
                # a stale token could name a replacement lifecycle, whose
                # metadata this caller has no authority to mutate.
                if error.kind != "local-io" or not self._removals.owns(intent):
                    return (f"removal journal: {error}",)
                journal_failure = f"removal journal: {error}"
            failures = list(
                self._cleanup_removed_item(
                    intent.item,
                    intent.roots,
                    artifacts_already_clean=artifacts_already_clean,
                    artifact_cleanup_deferred=artifact_cleanup_deferred,
                    artifact_source_root=artifact_source_root,
                    artifact_lookup_root=artifact_lookup_root,
                    artifact_lookup_parent=artifact_lookup_parent,
                    artifact_source_context=artifact_source_context,
                )
            )
            if failures:
                if journal_failure is not None:
                    failures.insert(0, journal_failure)
                return tuple(failures)
            try:
                self._removals.discard(intent)
            except removals.RemovalJournalError as error:
                return (f"removal journal: {error}",)
            return ()
        finally:
            self._removals.finish_operation(intent)

    def retry_removals(self, *, external_only: bool = False) -> tuple[str, ...]:
        """Replay crash-surviving intents without inferring from missing media."""
        intents = self._removals.reload()
        if self._removals.fault is not None:
            return (f"removal journal: {self._removals.fault}",)
        failures: list[str] = []
        operation_active: bool | None = None
        for intent in intents:
            if external_only and not intent.external:
                continue
            # A committed marker is authoritative even if settings changed or
            # the source filesystem went away afterwards. Availability is
            # only evidence for resolving the prepared-but-unmarked crash
            # window; it must never undo an explicit commit.
            if not intent.committed and any(
                root not in self._settings.roots for root in intent.roots
            ):
                failures.append(
                    f"{intent.item.name}: pending removal names a library root which is no "
                    "longer configured; restore that root and refresh to finish safely"
                )
                continue
            if not intent.committed:
                if operation_active is None:
                    try:
                        operation_active = self._removals.operation_is_active()
                    except removals.RemovalJournalError as error:
                        failures.append(f"{intent.item.name}: removal journal: {error}")
                        continue
                if operation_active:
                    failures.append(
                        f"{intent.item.name}: removal is still active in another process"
                    )
                    continue
                try:
                    source_context = intent.pin_source_context()
                except OSError, ValueError:
                    failures.append(
                        f"{intent.item.name}: the prepared library filesystem is unavailable "
                        "or changed; reconnect the same root and refresh before cleanup"
                    )
                    continue
                with source_context:
                    anchored_source = source_context.child(intent.path.name)
                    if not intent.external:
                        try:
                            recovered_claim = manage.recover_removal_claim(
                                intent.path,
                                expected_source=intent.source_identity,
                                expected_fingerprint=intent.source_fingerprint,
                                operation_token=intent.token,
                                lookup_path=anchored_source,
                            )
                        except manage.ManageError as error:
                            failures.append(f"{intent.item.name}: {error}")
                            continue
                        if recovered_claim:
                            current = self.commit_removal(
                                intent,
                                artifacts_already_clean=True,
                                artifact_source_root=intent.source_root,
                                artifact_lookup_root=source_context.root_anchor,
                                artifact_lookup_parent=source_context.directory_anchor,
                                artifact_source_context=source_context,
                            )
                            failures.extend(f"{intent.item.name}: {failure}" for failure in current)
                            continue
                    original_state = intent.original_generation_state(lookup_path=anchored_source)
                    if original_state == "exact":
                        # A crash before the physical operation left only a
                        # prepared intent. The exact original generation is still
                        # present, so this is safe to cancel and must not become
                        # missing-drive pruning.
                        try:
                            self._removals.discard(intent)
                        except removals.RemovalJournalError as error:
                            failures.append(f"{intent.item.name}: removal journal: {error}")
                        continue
                    if original_state == "ambiguous":
                        failures.append(
                            f"{intent.item.name}: pending removal cannot prove whether "
                            f"{intent.path} was safely restored or replaced after its claim; "
                            "the file, its metadata, and the removal journal were left untouched. "
                            "Move the preserved file aside or replace it with a fresh inode, then "
                            "refresh to finish the old cleanup"
                        )
                        continue
                    if original_state == "unavailable":
                        failures.append(
                            f"{intent.item.name}: pending removal could not safely inspect "
                            f"{intent.path}; restore access to that path and refresh before cleanup"
                        )
                        continue
                    current = self.commit_removal(
                        intent,
                        artifacts_already_clean=True,
                        artifact_source_root=intent.source_root,
                        artifact_lookup_root=source_context.root_anchor,
                        artifact_lookup_parent=source_context.directory_anchor,
                        artifact_source_context=source_context,
                    )
                    failures.extend(f"{intent.item.name}: {failure}" for failure in current)
                    continue
            # A committed lifecycle has no crash-surviving artifact-generation
            # pins. Clear authored metadata, but never rediscover and unlink
            # same-name files which may belong to a later installation.
            current = self.commit_removal(intent, artifacts_already_clean=True)
            failures.extend(f"{intent.item.name}: {failure}" for failure in current)
        return tuple(failures)

    def _cleanup_removed_item(
        self,
        item: MediaItem,
        roots: Sequence[Path],
        *,
        artifacts_already_clean: bool = False,
        artifact_cleanup_deferred: bool = False,
        artifact_source_root: Path | None = None,
        artifact_lookup_root: Path | None = None,
        artifact_lookup_parent: Path | None = None,
        artifact_source_context: file_io.PinnedDirectoryContext | None = None,
    ) -> tuple[str, ...]:
        """Attempt every durable association and deterministic child artifact."""
        failures: list[str] = []
        favourite_succeeded = False
        pairing_succeeded = False
        playlist_succeeded = False
        try:
            self._favourites.discard(item.path)
        except favourites.FavouritesError as error:
            failures.append(f"favourites: {error}")
        else:
            favourite_succeeded = self._favourites.fault is None
            if not favourite_succeeded:
                failures.append(f"favourites: {self._favourites.fault}")
        saved_pairing = self._pairings.get(pairings.Identity.of(item))
        legacy_selected = saved_pairing.still if saved_pairing is not None else None
        artifacts = manage.pairing_artifact_paths(
            item,
            roots,
            legacy_selected_still=legacy_selected,
        )
        try:
            self._pairings.forget_item(item, removed_stills=artifacts)
        except pairings.PairingError as error:
            failures.append(f"pairing: {error}")
        else:
            pairing_succeeded = self._pairings.fault is None
            if not pairing_succeeded:
                failures.append(f"pairing: {self._pairings.fault}")
        try:
            self._playlists.forget_path(item.path)
        except playlists.PlaylistError as error:
            failures.append(f"playlists: {error}")
        else:
            playlist_succeeded = self._playlists.fault is None
            if not playlist_succeeded:
                failures.append(f"playlists: {self._playlists.fault}")
        if artifact_cleanup_deferred:
            failures.append(
                "pairing files were not inspected because the prepared library filesystem "
                "is unavailable or changed"
            )
        elif not artifacts_already_clean:
            _discarded, retained = manage.discard_pairing_artifacts(
                item,
                roots,
                legacy_selected_still=legacy_selected,
                source_root=artifact_source_root,
                lookup_root=artifact_lookup_root,
                lookup_parent=artifact_lookup_parent,
                source_context=artifact_source_context,
            )
            if retained:
                names = ", ".join(path.name for path in retained[:3])
                failures.append(f"pairing files could not be removed: {names}")
        if self._cleanup_observer is not None:
            self._cleanup_observer(
                RemovalCleanup(
                    item=item,
                    removed_stills=artifacts,
                    favourite_succeeded=favourite_succeeded,
                    pairing_succeeded=pairing_succeeded,
                    playlist_succeeded=playlist_succeeded,
                )
            )
        return tuple(failures)

    def _forget_confirmed_workshop_removals(self, incoming: Library) -> tuple[MediaItem, ...]:
        """Forget an uninstall, but never confuse an unavailable drive with one.

        Workshop content is external and cannot be deleted here.  There is
        nevertheless one safe observation: an item which was present in the
        previous scan is gone while its Workshop *content directory* is still
        readable.  Steam removed that item rather than its whole library
        becoming unavailable.  That explicit lifecycle boundary clears the
        durable crash judgement and every authored reference, so reinstalling
        the same Workshop id starts clean.

        Turning Workshop scanning off is not an uninstall, nor is first run,
        so neither case enters this path.
        """
        removed: list[MediaItem] = []
        if self._settings.scan_workshop and self._settings.roots:
            incoming_identities = {
                pairings.Identity.of(item).key
                for item in incoming.items
                if item.provider == scan.WORKSHOP_PROVIDER
            }
            confirmed: set[str] = set()
            for identity, item in tuple(self._known_workshop.items()):
                identity = pairings.Identity.of(item).key
                if identity in incoming_identities:
                    continue
                # A video identity names its entry file, but Steam uninstalls
                # the containing Workshop-id directory. A missing/renamed
                # media file inside a still-installed item is not an
                # uninstall. Scenes already name that directory directly.
                installation = item.path if item.kind is Kind.SCENE else item.path.parent
                if os.path.lexists(installation):
                    continue
                content = installation.parent
                try:
                    # Opening the directory distinguishes a mounted, readable
                    # content root from one whose scan merely failed. Confirm
                    # the concrete id is absent in the same observation.
                    with os.scandir(content) as entries:
                        installation_absent = all(
                            entry.name != installation.name for entry in entries
                        )
                except OSError:
                    installation_absent = False
                if not installation_absent:
                    continue
                try:
                    self._removals.record_external(item, incoming.roots)
                except removals.RemovalJournalError:
                    # Steam already committed this uninstall. If the state
                    # directory itself is unavailable there is nowhere else
                    # to persist a trustworthy tombstone, so retain the
                    # in-process retry and surface any cleanup failure below.
                    self._pending_workshop_cleanup.setdefault(
                        identity,
                        _WorkshopCleanup(item, incoming.roots),
                    )
                removed.append(item)
                confirmed.add(identity)

            for identity in confirmed:
                self._known_workshop.pop(identity, None)
            for item in incoming.items:
                if item.provider == scan.WORKSHOP_PROVIDER:
                    identity = pairings.Identity.of(item).key
                    self._known_workshop[identity] = item

        failures = list(self.retry_removals(external_only=True))
        for identity, pending in tuple(self._pending_workshop_cleanup.items()):
            try:
                intent = self._removals.record_external(pending.item, pending.roots)
            except removals.RemovalJournalError as error:
                # Cleanup is still worth attempting: Steam has already
                # removed the install. Keep the in-process record even if all
                # stores succeed, though, and say why. Otherwise an unwritable
                # journal would be silently presented as a durable reset.
                item_failures = self._cleanup_removed_item(
                    pending.item,
                    pending.roots,
                    artifacts_already_clean=True,
                )
                failures.append(
                    f"{pending.item.name}: removal journal: {error}; repair the state "
                    "directory and refresh so this confirmed uninstall can be recorded durably"
                )
                failures.extend(f"{pending.item.name}: {failure}" for failure in item_failures)
                continue
            item_failures = self.commit_removal(intent, artifacts_already_clean=True)
            failures.extend(f"{pending.item.name}: {failure}" for failure in item_failures)
            # Once the journal accepted the lifecycle boundary, it owns any
            # remaining retry across process exit. The in-memory fallback is
            # no longer needed whether cleanup finished now or remains there.
            del self._pending_workshop_cleanup[identity]
        self._workshop_cleanup_failures = tuple(failures)
        return tuple(removed)

    @property
    def pairings(self) -> pairings.Store:
        """The customizations. The grid reads them; the applier obeys them."""
        return self._pairings

    def adopt_pairing_store(self, store: PairingStore) -> None:
        """Take ownership of a worker-finished durable Pairings snapshot.

        The graphical runtime-health bridge builds and mutates this store on
        its ordered I/O worker, then transfers it only after the worker has
        stopped touching it.  Rebuilding the compatibility cursor is pure and
        keeps an explicitly requested headless fallback coherent too.
        """
        self._pairings = store
        self._rebuild_playlist()

    @property
    def displays(self) -> displays.Store:
        """Which playlist each screen shows. Empty means "follow the default"."""
        return self._displays

    @property
    def schedules(self) -> schedules.Store:
        """The calendar rules that override the pinned playlist."""
        return self._schedules

    def active_playlist(self) -> str:
        """The one playlist in force: manual choice, schedule, then default."""
        if self._manual_playlist is not None:
            return self._manual_playlist
        return schedules.effective(
            self._schedules.rules, self._settings.active_playlist, self._now()
        )

    @property
    def manual_playlist(self) -> str | None:
        """The temporary on-demand override, or ``None`` while following time."""
        return self._manual_playlist

    @property
    def playlists(self) -> playlists.Store:
        """The named lists. The rotation follows whichever one is in force."""
        return self._playlists

    @property
    def favourites(self) -> favourites.Store:
        """The starred wallpapers. The grid reads it; the rotation obeys it."""
        return self._favourites

    def authoring_faults(self) -> tuple[tuple[str, str], ...]:
        """Unreadable authoring stores, named for a useful headless error.

        Store ``open`` methods deliberately recover to an empty in-memory
        value so the interactive application can still open and help somebody
        repair a damaged file. An unattended runtime-config compilation must
        make a different choice: compiling those empty values would silently
        replace the last known-good automation document.
        """
        stores = (
            ("pairings", self._pairings),
            ("playlists", self._playlists),
            ("schedules", self._schedules),
            ("display assignments", self._displays),
            ("favourites", self._favourites),
            ("pending removals", self._removals),
        )
        return tuple((name, fault) for name, store in stores if (fault := store.fault))

    def _rebuild_playlist(self) -> None:
        self._in_force = self.active_playlist()
        self._playlist.set_items(self._rotation(self._in_force))
        self._select_first_usable()

    def _rotation(self, active: str | None = None) -> tuple[MediaItem, ...]:
        """What `next` walks through.

        Narrowing to favourites is skipped when it would empty the rotation --
        favourites all deleted, or on a drive that is not mounted. A wallpaper
        manager that stops changing the wallpaper is a worse answer to "you
        have no favourites right now" than one that falls back to the library
        and keeps working; the setting is a preference about which wallpapers,
        not an instruction to show none.
        """
        playable = self._library.playable(dynamics_enabled=self._settings.dynamics_enabled)

        # A named playlist is the stronger statement, so it goes first:
        # somebody who built a list and then left "favourites only" on from
        # last week meant the list.
        listed = playlists.rotation(
            self._playlists,
            self.active_playlist() if active is None else active,
            playable,
        )
        if listed is not None:
            return listed

        if not self._settings.cycle_favourites_only:
            return playable
        starred = self._favourites.paths
        chosen = tuple(item for item in playable if item.path in starred)
        return chosen if chosen else playable

    def _is_usable(self, item: MediaItem) -> bool:
        return not self._pairings.health(pairings.Identity.of(item)).is_borked

    def _select_first_usable(self) -> bool:
        """Keep a Borked entry visible in authoring, but never cue it to play."""
        if not any(self._is_usable(item) for item in self._playlist.items):
            return False
        for _ in range(len(self._playlist)):
            current = self._playlist.current()
            if current is not None and self._is_usable(current):
                return True
            self._playlist.next()
        return False

    def _require_usable_rotation(self, reference: str, *, label: str) -> tuple[MediaItem, ...]:
        rotation = self._rotation(reference)
        if any(self._is_usable(item) for item in rotation):
            return rotation
        if rotation:
            raise ApplyError(f"{label} has no usable wallpapers; every item is marked Borked")
        raise ApplyError(f"{label} has no playable wallpapers in the current library")

    def schedule_changed(self) -> bool:
        """Rebuild if the calendar now asks for a different playlist.

        Answers whether anything moved, so the caller polling this on a timer
        can avoid redrawing a window every thirty seconds for nothing.
        """
        wanted = self.active_playlist()
        if wanted == self._in_force:
            return False
        try:
            rotation = self._require_usable_rotation(wanted, label="the scheduled playlist")
        except ApplyError:
            # Keep the last usable compatibility selection. The timer retries
            # after a later library/health edit instead of launching a known
            # crasher merely because the clock changed.
            return False
        self._in_force = wanted
        self._playlist.set_items(rotation)
        self._select_first_usable()
        return True

    def playlists_changed(self) -> None:
        """Re-narrow the rotation after a list was edited.

        Unconditional, like `favourites_changed`: only the active list changes
        the rotation, but working out whether the edited one *was* the active
        one is more code than rebuilding, and getting it wrong means a list
        that quietly does not take effect.
        """
        self._rebuild_playlist()

    def use_playlist(self, reference: str) -> NamedPlaylist:
        """Play one named list now, temporarily overriding schedule rules."""
        chosen = self._playlists.find(reference)
        rotation = self._require_usable_rotation(chosen.id, label=f"playlist {chosen.name!r}")
        self._manual_playlist = chosen.id
        self._in_force = chosen.id
        self._playlist.set_items(rotation)
        self._select_first_usable()
        return chosen

    def resume_schedule(self) -> None:
        """Release an on-demand choice and return control to the calendar."""
        wanted = schedules.effective(
            self._schedules.rules, self._settings.active_playlist, self._now()
        )
        rotation = self._require_usable_rotation(wanted, label="the scheduled playlist")
        self._manual_playlist = None
        self._in_force = wanted
        self._playlist.set_items(rotation)
        self._select_first_usable()

    def favourites_changed(self) -> None:
        """Re-narrow the rotation after a star moved.

        Only matters while `cycle_favourites_only` is on, but calling it
        unconditionally is what stops the rotation and the stars drifting
        apart the moment somebody turns the setting on later.
        """
        self._rebuild_playlist()

    def sync_with_noctalia(self) -> bool:
        """Move the cursor onto whatever wallpaper is actually up.

        Noctalia, or the user, can change the wallpaper behind our back. Without
        this, the next `next` jumps somewhere unrelated instead of continuing
        from what is on screen.
        """
        try:
            active = noctalia.current_wallpaper()
        except noctalia.NoctaliaError:
            return False
        return self.sync_to_wallpaper(active)

    def sync_to_wallpaper(self, active: Path | None) -> bool:
        """Adopt an already-queried live wallpaper without doing shell I/O.

        Graphical callers obtain ``active`` on their bounded worker and invoke
        this small state transition on GTK.  The synchronous compatibility
        service retains :meth:`sync_with_noctalia`, which delegates here after
        its own query.
        """
        if active is None:
            return False
        direct = self._library.find(active)
        if direct is not None and self._is_usable(direct) and self._playlist.select(active):
            return True
        # It may be a video's paired still rather than a library item itself.
        for item in self._playlist.items:
            if item.paired_still == active and self._is_usable(item):
                return self._playlist.select(item.path)
        return False

    # -- navigation ------------------------------------------------------

    def _apply(self, item: MediaItem | None) -> Applied:
        if item is None:
            raise ApplyError("the library is empty")
        if not self._is_usable(item):
            raise ApplyError(
                f"{item.name} is marked Borked and cannot play; "
                "remove or uninstall it before trying again"
            )
        # The pairing is resolved here rather than in the applier, because the
        # store is the session's and the applier has no business reading files.
        bundle = self._pairings.resolve(item, self._library.roots)
        return self._applier.apply(
            item,
            dynamics_enabled=self._settings.dynamics_enabled,
            palette=bundle.palette,
            generator=self._settings.preview_scheme,
        )

    def apply_current(self) -> Applied:
        return self._apply(self._playlist.current())

    def next(self) -> Applied:
        if not self._playlist.items:
            return self._apply(None)
        for _ in range(len(self._playlist)):
            item = self._playlist.next()
            if item is not None and self._is_usable(item):
                return self._apply(item)
        raise ApplyError("the active playlist has no usable wallpapers; every item is Borked")

    def previous(self) -> Applied:
        if not self._playlist.items:
            return self._apply(None)
        for _ in range(len(self._playlist)):
            item = self._playlist.previous()
            if item is not None and self._is_usable(item):
                return self._apply(item)
        raise ApplyError("the active playlist has no usable wallpapers; every item is Borked")

    def random(self) -> Applied:
        if not self._playlist.items:
            return self._apply(None)
        current = self._playlist.current()
        usable = [
            item
            for item in self._playlist.items
            if self._is_usable(item) and (current is None or item.path != current.path)
        ]
        if not usable:
            if current is not None and self._is_usable(current):
                return self._apply(current)
            raise ApplyError("the active playlist has no usable wallpapers; every item is Borked")
        chosen = self._rng.choice(usable)
        self._playlist.select(chosen.path)
        return self._apply(chosen)

    def choose(self, path: Path) -> NamedPlaylist:
        """Make ``path`` the visible one-entry playlist without applying it.

        The Python app is an authoring client now.  Keeping this step separate
        lets it compile the Quick choice into the Rust runtime config and ask
        that runtime to play it, instead of racing the service with its own
        applier.  ``select`` below remains the legacy-service fallback.
        """
        item = self._library.find(path)
        if item is None:
            raise ApplyError(f"not in the library: {path}")
        if not self._is_usable(item):
            raise ApplyError(
                f"{item.name} is marked Borked and cannot play; "
                "remove or uninstall it before trying again"
            )
        try:
            chosen = self._playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, item.path)
        except playlists.PlaylistError as error:
            raise ApplyError(str(error)) from error
        self.use_playlist(QUICK_CHOICE_ID)
        return chosen

    def select(self, path: Path) -> Applied:
        self.choose(path)
        return self.apply_current()

    # -- settings --------------------------------------------------------

    def update_settings(self, settings: config.Settings, *, rescan_library: bool = True) -> None:
        """Adopt new settings, reacting to the ones that change behaviour."""
        previous = self._settings
        self._settings = settings

        if settings.shuffle != previous.shuffle:
            self._playlist.set_shuffle(settings.shuffle)

        if settings.own_scene_renderer != previous.own_scene_renderer:
            self._applier.own_scene_renderer = settings.own_scene_renderer

        if settings.output != previous.output:
            # Both halves, because a still and a video reach the screen by
            # different routes and only one of them can be retuned live.
            self._applier.output = settings.output
            self._applier.renderer.output = settings.output or renderer.ALL_OUTPUTS
            self._applier.scenes.output = settings.output

        if (settings.cycle_favourites_only, settings.active_playlist) != (
            previous.cycle_favourites_only,
            previous.active_playlist,
        ):
            self._rebuild_playlist()

        if (settings.roots, settings.scan_workshop) != (
            previous.roots,
            previous.scan_workshop,
        ) and rescan_library:
            # Nothing else notices: the library is only re-read when something
            # asks, and a root or Workshop source the user just changed would
            # stay stale until the next launch. GUI callers opt out here and
            # submit the exact same scan through their background lane.
            self.refresh()

        if (settings.video_muted, settings.video_volume) != (
            previous.video_muted,
            previous.video_volume,
        ):
            # Over mpv's IPC, so the video keeps playing. Restarting mpvpaper
            # to change the volume would blink the wallpaper, which is a
            # ludicrous price for a slider.
            self._applier.renderer.apply_audio(
                muted=settings.video_muted, volume=settings.video_volume
            )

        if settings.video_when_hidden != previous.video_when_hidden:
            # This one is a launch flag of mpvpaper's, not an mpv property, so
            # it cannot be retuned live. Recording it is enough: the next video
            # starts under the new policy, and saying so beats restarting the
            # wallpaper underneath someone.
            self._applier.renderer.when_hidden = settings.video_when_hidden

        if (
            settings.video_hardware_decode,
            settings.video_interpolation,
        ) != (
            previous.video_hardware_decode,
            previous.video_interpolation,
        ):
            # Both are launch-time mpv options. The Rust runtime reloads and
            # reapplies the current entry; the Python compatibility renderer
            # records them for its next video without blinking this one.
            self._applier.renderer.hardware_decode = settings.video_hardware_decode
            self._applier.renderer.interpolation = settings.video_interpolation

        if (
            settings.scene_fps,
            settings.scene_scaling,
            settings.scene_clamp,
        ) != (
            previous.scene_fps,
            previous.scene_scaling,
            previous.scene_clamp,
        ):
            # This is a launch-time linux-wallpaperengine setting. The Rust
            # runtime reloads the compiled config and performs the active
            # hand-over; this legacy fallback adopts it for the next scene.
            self._applier.scenes.fps = settings.scene_fps
            self._applier.scenes.scaling = settings.scene_scaling
            self._applier.scenes.clamp = settings.scene_clamp

        if settings.dynamics_enabled != previous.dynamics_enabled:
            # Pausing a video with no still used to mean jumping to an unrelated
            # wallpaper. Take a still from it first: a third of a second of
            # ffmpeg, once per video, against the thing the user is watching
            # being swapped out from under them.
            rescued = (
                None if settings.dynamics_enabled else self._still_for_the_video_being_paused()
            )
            # The playable set changes with dynamics: videos with no still drop
            # out when they are off, and come back when they are on.
            self._rebuild_playlist()
            if self._applier.set_dynamics(settings.dynamics_enabled) is None:
                # The wallpaper that was up cannot be shown any more -- an
                # unpaired video being paused. Show its new still if we just
                # made one, and otherwise fall back to whatever the playlist
                # landed on rather than leaving a dead screen.
                with contextlib.suppress(ApplyError):
                    if rescued is not None:
                        self.select(rescued)
                    else:
                        self.apply_current()

    def _still_for_the_video_being_paused(self) -> Path | None:
        """Take a still from the playing video, and return the video's path.

        ``None`` when there is nothing to do or nothing can be done -- already
        paired, not a video, no root to write into, or ffmpeg refusing the
        file. None of those is worth reporting: the fallback below still puts
        *a* wallpaper on screen, which is what pausing has always done.

        The rescan is what makes the new still count. `paired_still` is fixed
        on the `MediaItem` at scan time, so the library has to be re-read
        before anything downstream can see the pairing that was just written.
        """
        current = self._applier.current
        if current is None or current.item.kind is not Kind.VIDEO:
            return None
        if current.item.paired_still is not None:
            return None
        roots = self._library.roots
        if not roots:
            return None
        if stills.ensure(current.item, roots[0]) is None:
            return None
        self.refresh()
        return current.item.path

    def shutdown(self) -> None:
        self._removals.close()
        self._applier.shutdown()

    # -- reporting -------------------------------------------------------

    def describe(self) -> str:
        current = self.current
        showing = current.describe() if current is not None else "nothing applied"
        return (
            f"{showing}; {len(self._playlist)} of {len(self._library)} playable; "
            f"shuffle={'on' if self._settings.shuffle else 'off'} "
            f"cycle={'on' if self._settings.cycle_enabled else 'off'} "
            f"cycle-interval={self._settings.cycle_interval} "
            f"dynamics={'on' if self._settings.dynamics_enabled else 'off'}"
        )
