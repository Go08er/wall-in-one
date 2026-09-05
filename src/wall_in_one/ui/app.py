"""Application object: wires the palette, the stylesheet, and the control socket."""

from __future__ import annotations

import json
import sys
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import config, file_io, legacy_migration, paths, runtime_config, runtime_health
from wall_in_one.browse import Browser, source_page_url
from wall_in_one.control import client, server
from wall_in_one.control.protocol import Response
from wall_in_one.library import (
    displays,
    favourites,
    manage,
    pairings,
    playlists,
    removals,
    scan,
    schedules,
)
from wall_in_one.library import filter as library_filter
from wall_in_one.library.model import Library, MediaItem
from wall_in_one.providers import registry
from wall_in_one.providers.base import SearchQuery, WallpaperCandidate
from wall_in_one.session import (
    QUICK_CHOICE_ID,
    QUICK_CHOICE_NAME,
    LibraryRefreshBusyError,
    LibraryRefreshCancelledError,
    LibraryRefreshPlan,
    LibraryRefreshResult,
    RemovalResult,
    Session,
)
from wall_in_one.theme import css, noctalia, source
from wall_in_one.ui.stills import StillMaker
from wall_in_one.ui.window import ACCELERATORS, MainWindow
from wall_in_one.wallpaper import outputs
from wall_in_one.wallpaper.applier import Applied, ApplyError


def download_root(settings: config.Settings) -> Path | None:
    """Where a download from the control socket lands, or None to refuse it.

    The first configured root, because that is the one the user put first --
    which is exactly what `ui.browse_dialog` does with its own `Browser`, and
    the two paths have to agree or the same wallpaper would arrive in different
    directories depending on which of them asked for it. With none configured,
    Browser reports that Settings needs a choice instead of inferring one.
    """
    configured = settings.roots
    return configured[0] if configured else None


#: How often the calendar is re-read. A minute, because that is the
#: resolution schedule rules are written at.
SCHEDULE_TICK_SECONDS: Final = 60
RUNTIME_STATUS_TICK_SECONDS: Final = 2
PALETTE_RELOAD_DEBOUNCE_MS: Final = 75
MAX_GUI_AUTHORING_QUEUE: Final = 32
PACKAGE_SOURCE_ACTION: Final = "package-source"
PRESENT_PACKAGE_ACTION: Final = "present-package"
# Installed Nix package directories are immutable and distinguish revisions
# even while their human-readable version strings are equal. This is source
# identity, not a promise that a mutable development checkout is unchanged.
PACKAGE_SOURCE: Final = str(Path(__file__).resolve().parent.parent)

# GTK reads Noctalia's imported gtk.css at user priority only at startup, while
# this provider is refreshed after every palette render. Both carry the same
# palette, so the copy we can guarantee is current must win inside this process.
APPLICATION_STYLE_PRIORITY: Final = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
_RuntimeResult = TypeVar("_RuntimeResult")
_AuthoringResult = TypeVar("_AuthoringResult")
_LibrarySources = tuple[tuple[Path, ...], bool]
PaletteReloadCallback = Callable[[source.ResolvedPalette | None, str], None]
ThemeActionCallback = Callable[[str], None]
_LegacyMigrationOperation = Literal["probe", "later", "fresh", "import"]
_AuthoringGateState = Literal["checking", "decision", "probe-failed", "repair"]


@dataclass(frozen=True, slots=True)
class _LegacyMigrationRequest:
    """One filesystem migration operation, with no GTK objects attached."""

    generation: int
    window_generation: int
    operation: _LegacyMigrationOperation
    found: legacy_migration.Probe | None = None


@dataclass(frozen=True, slots=True)
class _LegacyMigrationResult:
    """Worker-owned migration data which is safe to hand back to GTK."""

    request: _LegacyMigrationRequest
    found: legacy_migration.Probe | None = None
    outcome: legacy_migration.Outcome | None = None
    settings: config.Settings | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _PaletteRequest:
    """An immutable live-theme snapshot safe for the worker to inspect."""

    generation: int
    follow_noctalia: bool
    scheme: str


@dataclass(frozen=True, slots=True)
class _PaletteResult:
    """One worker result; GTK consumes it only through ``GLib.idle_add``."""

    generation: int
    resolved: source.ResolvedPalette | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _ThemeAction:
    """One ordered shell palette selection, containing no GTK objects."""

    generation: int
    palette_source: str
    name: str


@dataclass(frozen=True, slots=True)
class _ThemeActionResult:
    generation: int
    error: str = ""


@dataclass(frozen=True, slots=True)
class _AuthoringTask:
    """One transaction held until its GTK/runtime tail has replied.

    ``prepare`` is deliberately GTK-owned and runs only when this task reaches
    the head of the actor.  State-dependent commands must resolve names and
    toggles there, after every earlier transaction has been adopted, rather
    than capturing a stale Session while an older write is still queued.
    """

    work: Callable[[], Any]
    finish: Callable[[Any], server.Outcome]
    reply: server.Reply
    prepare: Callable[[], Callable[[], Any]] | None = None
    finalize: Callable[[], None] | None = None
    requires_migration: bool = True
    guard_migration_transaction: bool = True
    application_held: bool = False


@dataclass(frozen=True, slots=True)
class _WallpaperQueryResult:
    """Noctalia's active path for one accepted library-scan generation."""

    scan_generation: int
    active: Path | None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _RuntimeAuthoringRequest:
    """The only application state allowed to cross into the runtime worker.

    ``Library`` and all of its members are frozen value objects.  Settings and
    the JSON authoring stores are deliberately *not* captured here: the worker
    reloads their latest durable versions while it owns the cross-process
    compiler lock.  This keeps a live, mutable :class:`Session` confined to
    GTK's thread without copying a potentially large playlist on every click.
    """

    generation: int
    library: Library
    library_sources: _LibrarySources


@dataclass(frozen=True, slots=True)
class _RuntimePublication:
    """One coalesced compiler/reload result delivered back to GTK."""

    generation: int
    changed: bool = False
    response: Response | None = None
    error: str = ""
    unavailable: bool = False
    deferred: bool = False


class _RuntimeLibraryNotReadyError(Exception):
    """The accepted Library belongs to older source settings than disk."""


@dataclass(frozen=True, slots=True)
class _RuntimeStatusReply:
    """A socket response parsed away from GTK's main context."""

    response: Response
    status: dict[str, object] | None = None
    status_document: str = ""
    protocol_error: str = ""


@dataclass(frozen=True, slots=True)
class _RuntimeHealthRequest:
    """Immutable inputs for one guarded runtime-health transaction."""

    authoring_generation: int
    library: Library
    status_document: str


@dataclass(frozen=True, slots=True)
class _RuntimeHealthResult:
    """Durable health result whose Store ownership can move back to GTK."""

    request: _RuntimeHealthRequest
    changed: int = 0
    accepted: bool = False
    store: pairings.Store | None = None
    publication: _RuntimePublication | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _StillPairingMutationResult:
    """Durable choice plus the default resolved on its authoring worker."""

    item: MediaItem
    record: pairings.Pairing
    effective_still: Path | None


@dataclass(frozen=True, slots=True)
class _PairingResetMutationResult:
    """Durable reset plus its worker-resolved effective default."""

    item: MediaItem
    changed: bool
    effective_still: Path | None


@dataclass(frozen=True, slots=True)
class _SettingsResult:
    """The newest coalesced settings snapshot which actually reached disk."""

    generation: int
    settings: config.Settings
    changes: tuple[tuple[str, int, Any], ...]


@dataclass(frozen=True, slots=True)
class _PlaylistDeleteResult:
    """A committed playlist deletion plus every independently durable tail."""

    playlist_id: str
    name: str
    settings: config.Settings | None = None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PlaylistReferenceRepairResult:
    """Idempotent cleanup of references left by an interrupted deletion."""

    settings: config.Settings | None = None
    schedule_playlists: tuple[str, ...] = ()
    display_playlists: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


class Application(Adw.Application):
    """Owns app-wide state: settings, the live palette, and the CSS provider."""

    def __init__(self, *, service: bool = False, initial_page: str | None = None) -> None:
        super().__init__(
            application_id=paths.APPLICATION_ID,
            # IS_SERVICE suppresses the initial activation, so `--service`
            # starts the singleton without constructing a window. A later
            # ordinary invocation with the same application id is forwarded
            # to this process as an activation and presents the GUI.
            flags=(
                Gio.ApplicationFlags.IS_SERVICE if service else Gio.ApplicationFlags.DEFAULT_FLAGS
            ),
        )
        identity = Gio.SimpleAction.new_stateful(
            PACKAGE_SOURCE_ACTION, None, GLib.Variant("s", PACKAGE_SOURCE)
        )
        identity.set_enabled(False)
        # Disabled prevents activation, not change-state. Consume those
        # requests explicitly so a desktop action client cannot rewrite the
        # identity that later launchers use for generation checks.
        identity.connect("change-state", lambda _action, _value: None)
        self.add_action(identity)
        present = Gio.SimpleAction.new(PRESENT_PACKAGE_ACTION, GLib.VariantType.new("(ss)"))
        present.connect("activate", self._present_from_package)
        self.add_action(present)
        self._service_start = service
        self._initial_page = initial_page
        self._held = False
        self._settings = config.load()
        self._window: MainWindow | None = None
        # An unresolved first run is asked once per graphical process. A real
        # choice persists naturally as the first configured root; dismissing
        # does not invent a durable "asked" bit and is offered again next run.
        self._library_root_prompt: Adw.AlertDialog | None = None
        self._library_root_prompted = False
        # Checked before the library-root question.  "Not now" is deliberately
        # process-local; Start fresh/Keep current is the explicit durable
        # disposition written by legacy_migration.decline().
        self._legacy_migration_prompt: Adw.AlertDialog | None = None
        self._legacy_migration_prompted = False
        self._legacy_migration_deferred = False
        # Probing the predecessor can read several documents, while importing
        # stages, links and fsyncs a complete profile.  Neither is permitted on
        # GTK's main context.  One lazy worker gives the transaction a strict
        # order and the tracked Future prevents duplicate dialog responses
        # from starting a second import.
        self._legacy_migration_jobs: ThreadPoolExecutor | None = None
        self._legacy_migration_future: Future[_LegacyMigrationResult] | None = None
        self._legacy_migration_generation = 0
        self._legacy_migration_retry_probe = False
        self._legacy_migration_shutdown = False
        # The control socket starts before the first graphical activation.
        # Until the asynchronous predecessor probe proves there is no decision
        # to make, no current-profile mutation may race a safe import.  This is
        # monotonic for the process: only a successful no-legacy probe, durable
        # Start fresh decision, or completed import opens it.
        self._authoring_migration_ready = False
        # A closed gate is not necessarily a migration decision.  The initial
        # predecessor probe and the current-profile consistency repair are
        # transient startup work; only a confirmed predecessor/probe failure
        # may tell a control client to ask the user for a decision.
        self._authoring_gate_state: _AuthoringGateState = "checking"
        self._authoring_repair_started = False
        self._headless_authoring_started = False
        self._activation_waiting_for_repair = False
        self._provider = Gtk.CssProvider()
        self._control: server.SocketServer | None = None
        self._resolved: source.ResolvedPalette | None = None
        self._session = Session(self._settings)
        self._cycle_source: int = 0
        self._schedule_source: int = 0
        self._runtime_status_source: int = 0
        # Every GUI-originated runtime request shares one worker.  Apart from
        # keeping socket deadlines off GTK's main thread, one worker preserves
        # command order: a configuration reload queued before ``playlist-use``
        # cannot be overtaken by the playback command it enables.
        self._runtime_jobs: ThreadPoolExecutor | None = None
        self._runtime_cancellation = client.Cancellation()
        # Control-socket authoring writes may wait five seconds for another
        # process's state lock and every successful atomic replacement fsyncs
        # both its file and parent directory.  They therefore have their own
        # ordered lane instead of borrowing GTK's main context.  Jobs receive
        # only a Store plus frozen/value inputs; the live Session and GTK
        # objects remain on the main thread and are reconciled by the finish
        # callback.
        self._authoring_jobs: ThreadPoolExecutor | None = None
        self._authoring_shutdown = False
        self._authoring_active = False
        self._authoring_queue: deque[_AuthoringTask] = deque()
        # A normal graphical GApplication would otherwise exit as soon as its
        # last window closes, even while a durable worker is between fsync and
        # GTK adoption.  Every accepted actor transaction owns one application
        # hold through its complete adoption/runtime tail.
        self._authoring_lifetime_holds = 0
        self._quit_requested = False
        self._removal_active = False
        self._removal_runtime_invalidated = False
        # A committed Delete/Trash retains a second hold until a post-delete
        # scan has reached a terminal result and the filtered runtime snapshot
        # has been compiled.  The actor hold alone ends too early: adoption
        # intentionally starts that convergence tail asynchronously.
        self._removal_convergence_held = False
        self._removal_convergence_scan_generation: int | None = None
        self._removal_convergence_scan_terminal = False
        self._removal_convergence_runtime_target = 0
        self._removal_convergence_runtime_terminal = False
        self._refresh_after_removal = False
        self._removed_item_paths: set[Path] = set()
        # Preferences notifications can arrive faster than one fsync.  Keep
        # one actor transaction alive until it has persisted the newest field
        # union; immutable Settings snapshots cross back to GTK only after the
        # final durable generation.  The lock protects the tiny dict/generation
        # shared with the worker, never GTK objects or Session.
        self._settings_authoring_lock = threading.Lock()
        self._settings_authoring_generation = 0
        self._settings_authoring_pending: dict[str, tuple[int, Any]] = {}
        self._settings_authoring_running = False
        self._settings_authoring_callbacks: list[
            tuple[
                int,
                Callable[[config.Settings], None] | None,
                Callable[[str], None] | None,
            ]
        ] = []
        self._settings_requested = self._settings
        self._runtime_status_pending = False
        self._runtime_status_generation = -1
        self._runtime_status_again = False
        self._runtime_health_pending = False
        self._runtime_health_request: _RuntimeHealthRequest | None = None
        # The Rust snapshot is the sole playback truth for every GUI surface.
        # Keep the last valid answer through a transient deadline so the
        # header and authoring pages cannot briefly contradict one another.
        self._runtime_status: dict[str, object] | None = None
        self._taboo_omitted_seen = 0
        self._runtime_action_pending = False
        self._quick_choice_pending = False
        self._runtime_reload_pending = False
        self._runtime_reload_again = False
        self._runtime_config_lock = threading.Lock()
        # Worker-only lifecycle exclusion.  Runtime compilation/reload holds
        # this across the socket acknowledgement; a removal takes it before
        # acquiring the migration marker and compiler lock.  GTK only flips
        # invalidation flags and never waits on this lock.
        self._runtime_removal_lock = threading.Lock()
        # Publication requests contain only an immutable accepted Library.
        # The ordered runtime worker reloads every disk-backed authoring store
        # under ``compiler_lock`` and folds bursts into the newest generation.
        self._runtime_authoring_generation = 0
        self._runtime_compiled_generation = 0
        self._runtime_compile_pending = False
        self._runtime_authoring_request: _RuntimeAuthoringRequest | None = None
        # Runtime publication outlives the authoring actor callback which
        # queued it.  Keep one coalesced generation-target hold so closing the
        # last normal window cannot cancel a committed Store -> runtime.toml
        # transition between those two durability boundaries.
        self._runtime_publication_held = False
        self._runtime_publication_hold_target = 0
        self._accepted_library_sources: _LibrarySources | None = None
        self._runtime_library_scan_generation = 0
        self._runtime_library_scan_sources: _LibrarySources | None = None
        self._runtime_library_scan_pending = False
        self._runtime_library_error = ""
        # A source-changing Settings write is not publishable until its
        # Library scan settles. The settings actor can finish before a runtime
        # publication hold exists, so keep one generation-owned application
        # hold across that gap.
        self._runtime_library_scan_held = False
        self._runtime_library_scan_hold_generation = 0
        self._runtime_library_ready = threading.Event()
        self._runtime_library_ready.set()
        self._runtime_document_generation = 0
        self._runtime_loaded_generation = 0
        self._runtime_shutdown = False
        # A GTK object can be destroyed while a worker is waiting on the
        # socket, then replaced by a later activation.  The monotonically
        # increasing generation keeps that old answer away from the new
        # window even when Python happens to reuse an object address.
        self._window_generation = 0
        self._palette_monitor: Gio.FileMonitor | None = None
        self._noctalia_settings_monitor: Gio.FileMonitor | None = None
        self._palette_reload_source: int = 0
        # Live palette resolution may make several 10-second shell IPC calls
        # and one 30-second generator call.  A single application-owned worker
        # keeps every one off GTK while preserving the order of explicit
        # palette choices.  Monitor/post-hook reload bursts coalesce to the
        # latest immutable settings snapshot; explicit choices always run
        # ahead of a merely pending repaint.
        self._theme_jobs: ThreadPoolExecutor | None = None
        self._theme_lock = threading.Lock()
        self._theme_cancelled = threading.Event()
        self._theme_draining = False
        self._theme_shutdown = False
        self._theme_generation = 0
        self._theme_action_generation = 0
        self._theme_actions: deque[_ThemeAction] = deque()
        self._theme_pending_wallpaper_scan: int | None = None
        self._theme_pending_palette: _PaletteRequest | None = None
        self._theme_reload_callbacks: list[PaletteReloadCallback] = []
        self._theme_action_callbacks: dict[int, ThemeActionCallback] = {}
        self._suppress_palette_reload = 0
        self._browse_jobs: ThreadPoolExecutor | None = None
        self._browse_lock = threading.Lock()
        self._browse_shutdown = False
        self._browse_transports: set[Browser] = set()
        # Library walking is filesystem I/O, not the measured pure-Python
        # parsing handled by the shared subinterpreter pool. One thread keeps
        # GTK responsive without paying another interpreter's memory cost or
        # making injected/headless scanners cross a pickling boundary.
        self._library_scan_jobs: ThreadPoolExecutor | None = None
        self._library_scan_future: Future[Library] | None = None
        self._library_scan_request: LibraryRefreshPlan | None = None
        self._library_scan_generation = 0
        self._library_scan_shutdown = False
        self._stills = StillMaker(report=self.window_report)

    # -- lifecycle -------------------------------------------------------

    def _present_from_package(self, _action: Gio.SimpleAction, value: GLib.Variant) -> None:
        expected, page = value.unpack()
        if expected != PACKAGE_SOURCE:
            # Ownership can change between a remote identity read and its
            # activation request. Never let that race open an unintended build.
            return
        if page:
            self.present_page(page)
        else:
            self.activate()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # The process, rather than the last window, owns rotation and the
        # calendar. Exactly one hold is released by `ctl quit`; closing every
        # window therefore leaves the service and its timers alive.
        if self._service_start:
            self.hold()
            self._held = True
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._provider,
                APPLICATION_STYLE_PRIORITY,
            )
        # Bind the private control socket before submitting any background
        # work: its creation uses one narrowly scoped process umask so the
        # filesystem socket is mode 0600 from its first visible instant.
        self._start_control_socket()
        self._start_palette_monitor()
        self._install_accelerators()
        self._start_windowless_migration_probe()

    def _open_authoring_after_migration(self) -> None:
        """Repair interrupted playlist tails before opening profile writes."""
        if self._authoring_migration_ready or self._authoring_repair_started:
            return
        self._authoring_gate_state = "checking"
        self._authoring_repair_started = True

        deferred = self.authoring_off_thread(
            self._repair_dangling_playlist_references,
            self._finish_playlist_reference_repair,
            requires_migration=False,
            queue_if_busy=True,
        )

        def replied(response: Response) -> None:
            if not response.ok and not self._authoring_shutdown:
                self.window_report(response.message)

        deferred.start(replied)

    @staticmethod
    def _repair_dangling_playlist_references() -> _PlaylistReferenceRepairResult:
        """Remove only references whose playlist identity is durably absent."""
        playlist_store = playlists.Store.open()
        if playlist_store.fault is not None:
            return _PlaylistReferenceRepairResult(failures=(f"playlists: {playlist_store.fault}",))
        valid = frozenset(playlist.id for playlist in playlist_store.all())
        failures: list[str] = []
        repaired_schedules: list[str] = []
        repaired_displays: list[str] = []

        schedule_store = schedules.Store.open()
        if schedule_store.fault is not None:
            failures.append(f"schedule rules: {schedule_store.fault}")
        else:
            dangling = sorted({rule.playlist for rule in schedule_store.rules} - valid)
            for playlist_id in dangling:
                try:
                    schedule_changed = schedule_store.forget_playlist(playlist_id)
                except schedules.ScheduleError as error:
                    failures.append(f"schedule rules for {playlist_id}: {error}")
                else:
                    if schedule_changed:
                        repaired_schedules.append(playlist_id)

        display_store = displays.Store.open()
        if display_store.fault is not None:
            failures.append(f"display assignments: {display_store.fault}")
        else:
            dangling = sorted({playlist for _connector, playlist in display_store.all()} - valid)
            for playlist_id in dangling:
                try:
                    display_count = display_store.forget_playlist(playlist_id)
                except displays.DisplayError as error:
                    failures.append(f"display assignments for {playlist_id}: {error}")
                else:
                    if display_count:
                        repaired_displays.append(playlist_id)

        saved_settings: config.Settings | None = None
        try:
            durable_settings = config.load_strict()
            active = durable_settings.active_playlist
            if active and active not in valid:
                saved_settings = config.forget_playlist_default(active)
                if saved_settings is None:
                    durable_settings = config.load_strict()
                    if (
                        durable_settings.active_playlist
                        and durable_settings.active_playlist not in valid
                    ):
                        failures.append(
                            "saved default changed while dangling playlist repair was running"
                        )
        except config.ConfigError as error:
            failures.append(f"saved default: {error}")

        return _PlaylistReferenceRepairResult(
            settings=saved_settings,
            schedule_playlists=tuple(repaired_schedules),
            display_playlists=tuple(repaired_displays),
            failures=tuple(failures),
        )

    def _finish_playlist_reference_repair(
        self,
        result: _PlaylistReferenceRepairResult,
    ) -> Response:
        """Adopt committed repair deltas, opening the gate only when clean."""
        self._session.schedules.adopt_worker_forget_playlists(result.schedule_playlists)
        self._session.displays.adopt_worker_forget_playlists(result.display_playlists)
        if result.settings is not None:
            previous = self._settings
            with self._settings_authoring_lock:
                self._settings_requested = result.settings
            self._settings = result.settings
            self._session.update_settings(result.settings, rescan_library=False)
            if self._window is not None:
                self._window.apply_settings(result.settings)
            if previous.active_playlist != result.settings.active_playlist:
                self._session.playlists_changed()
        elif result.schedule_playlists:
            self._session.playlists_changed()

        if result.failures:
            self._authoring_repair_started = False
            self._authoring_gate_state = "repair"
            return Response.failure(
                "current authoring has dangling playlist references which could not be "
                "repaired; writes remain paused: " + "; ".join(result.failures[:3]),
                kind="authoring-repair-required",
            )

        self._authoring_migration_ready = True
        if self._service_start and not self._headless_authoring_started:
            self._headless_authoring_started = True
            # A partial import may already have linked Settings roots while
            # its journal is still resumable. Timers, scan cleanup and
            # automatic still generation remain inert until both checks pass.
            self.sync_cycle_timer()
            self._start_schedule_timer()
            self.refresh_library()
        if self._activation_waiting_for_repair and self._window is not None:
            self._activation_waiting_for_repair = False
            self._continue_first_activation()
        return Response.success("authoring references are consistent")

    def _start_windowless_migration_probe(self) -> None:
        """Open headless authoring only after a worker proves it is safe.

        ``--service`` may run for days before a GUI is forwarded to it.  It
        cannot remain permanently gated when there is no predecessor state,
        but the probe itself performs filesystem reads and therefore does not
        belong in startup's GTK callback.
        """
        if self._legacy_migration_shutdown or self._legacy_migration_future is not None:
            return
        self._legacy_migration_generation += 1
        request = _LegacyMigrationRequest(
            self._legacy_migration_generation,
            0,
            "probe",
        )
        try:
            future = self._legacy_migration_pool().submit(
                self._run_legacy_migration_job,
                request,
            )
        except RuntimeError:
            return
        self._legacy_migration_future = future
        future.add_done_callback(
            lambda done: GLib.idle_add(self._finish_legacy_migration_job, request, done)
        )

    def _install_accelerators(self) -> None:
        """Bind the keys. The table lives with the window, which owns the actions.

        Several accelerators may share one action, so they are collected per
        action before being set -- `set_accels_for_action` replaces the list
        rather than adding to it, and binding them one at a time would leave
        only the last.
        """
        bound: dict[str, list[str]] = {}
        for _section, accelerator, action, _description in ACCELERATORS:
            bound.setdefault(action, []).append(accelerator)
        for action, accelerators in bound.items():
            self.set_accels_for_action(action, accelerators)

    def do_activate(self) -> None:
        if self._window is None:
            window = MainWindow(self, self._settings)
            window.connect("close-request", self._on_close_request)
            self._window = window
            self._window_generation += 1
            if self._runtime_action_pending:
                window.set_runtime_busy(True)
            if self._runtime_status is not None:
                window.show_runtime_status(self._runtime_status)
        assert self._window is not None
        self._window.present()
        self._start_runtime_status_timer()
        if self._initial_page is not None:
            page = {
                "displays": "schedules",
                "pairings": "media",
            }.get(self._initial_page, self._initial_page)
            self._window.show_page(page)
            self._initial_page = None
        if self._prompt_for_legacy_migration():
            return
        self._continue_first_activation()

    def _continue_first_activation(self) -> None:
        """Scan or ask for a root only after legacy authoring has a disposition."""
        if not self._authoring_migration_ready:
            self._activation_waiting_for_repair = True
            return
        self.reload_palette()
        if self._settings.roots:
            self.refresh_library()
        else:
            self._prompt_for_library_root()

    def _prompt_for_legacy_migration(self) -> bool:
        """Check for predecessor data without reading it on GTK's thread."""
        if self._window is None or self._legacy_migration_prompt is not None:
            return self._legacy_migration_prompt is not None
        if self._legacy_migration_prompted:
            return self._legacy_migration_deferred
        self._legacy_migration_prompted = True
        if self._legacy_migration_future is not None:
            # A former window can close while its worker is blocked.  Do not
            # run two transactions: show progress for this window, discard the
            # old generation when it lands, then enqueue one fresh probe.
            self._legacy_migration_retry_probe = True
            self._show_legacy_migration_progress(
                "Checking older data",
                "Waiting for the previous safe check to finish…",
            )
            return True
        self._show_legacy_migration_progress(
            "Checking older data",
            "Looking for settings and playlists from the previous Noctalia plugin…",
        )
        if not self._queue_legacy_migration_job("probe"):
            self._close_legacy_migration_dialog()
            self._legacy_migration_prompted = False
            return False
        return True

    def _present_legacy_migration_choice(
        self,
        found: legacy_migration.Probe,
        *,
        failure: str = "",
    ) -> None:
        """Present one actionable, generation-owned migration decision."""
        if self._window is None or self._legacy_migration_shutdown:
            return
        details = (
            f"\n\nSource: {found.source}"
            + (f"\nSaved data format: {found.schema}" if found.schema is not None else "")
            + (
                f"\nPlaylists: {found.playlists} · displays: {found.outputs}"
                if found.schema is not None
                else ""
            )
        )
        conflict_text = ""
        if found.conflicts:
            shown = "\n".join(str(path) for path in found.conflicts[:4])
            conflict_text = (
                "\n\nCurrent settings or playlists already exist and will not be overwritten or "
                f"merged automatically:\n{shown}"
            )
        body = (
            "Wall-in-One found settings, playlists, and display assignments from the previous "
            "Noctalia plugin. Importing keeps the original files and adds them only when "
            "this app has no saved settings or playlists. "
            f"{found.detail}.{details}{conflict_text}"
        )
        if failure:
            body = f"{failure}\n\n{body}"
        dialog = Adw.AlertDialog(heading="Older Wall-in-One data found", body=body)
        dialog.add_response("later", "Not now")
        if found.status in ("ready", "in-progress"):
            import_label = "Resume import" if found.status == "in-progress" else "Import safely"
            dialog.add_response("import", import_label)
            dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("import")
        # A surviving journal is an authoritative partial transaction.  The
        # backend deliberately refuses decline in that state, so advertising
        # Start fresh would be both misleading and unsafe.
        if found.status != "in-progress":
            keep_label = "Keep current" if found.status == "conflict" else "Start fresh"
            dialog.add_response("fresh", keep_label)
        dialog.set_close_response("later")
        dialog.connect("response", self._on_legacy_migration_response, found)
        self._legacy_migration_prompt = dialog
        dialog.present(self._window)

    def _show_legacy_migration_progress(self, heading: str, body: str) -> None:
        """Show a modal progress state which cannot start another operation."""
        if self._window is None:
            return
        dialog = Adw.AlertDialog(heading=heading, body=body)
        spinner = Adw.Spinner()
        spinner.set_tooltip_text(heading)
        dialog.set_extra_child(spinner)
        dialog.add_response("working", "Working…")
        dialog.set_response_enabled("working", False)
        dialog.set_can_close(False)
        self._legacy_migration_prompt = dialog
        dialog.present(self._window)

    def _close_legacy_migration_dialog(self) -> None:
        dialog = self._legacy_migration_prompt
        self._legacy_migration_prompt = None
        if dialog is not None:
            dialog.force_close()

    def _legacy_migration_pool(self) -> ThreadPoolExecutor:
        """Return the sole ordered worker used by the one-time migration UI."""
        if self._legacy_migration_jobs is None:
            self._legacy_migration_jobs = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="legacy-migration",
            )
        return self._legacy_migration_jobs

    def _queue_legacy_migration_job(
        self,
        operation: _LegacyMigrationOperation,
        found: legacy_migration.Probe | None = None,
    ) -> bool:
        """Submit one migration operation; a second response is a no-op."""
        if (
            self._legacy_migration_shutdown
            or self._window is None
            or self._legacy_migration_future is not None
        ):
            return False
        self._legacy_migration_generation += 1
        request = _LegacyMigrationRequest(
            self._legacy_migration_generation,
            self._window_generation,
            operation,
            found,
        )
        try:
            future = self._legacy_migration_pool().submit(
                self._run_legacy_migration_job,
                request,
            )
        except RuntimeError:
            return False
        self._legacy_migration_future = future
        # A successfully admitted probe/choice is now processing, even when
        # it follows a previously confirmed decision or probe failure.  If it
        # fails or still finds unsafe predecessor state, the GTK completion
        # below restores ``decision`` before another mutation can be admitted.
        self._authoring_gate_state = "checking"
        future.add_done_callback(
            lambda done: GLib.idle_add(self._finish_legacy_migration_job, request, done)
        )
        return True

    @staticmethod
    def _run_legacy_migration_job(
        request: _LegacyMigrationRequest,
    ) -> _LegacyMigrationResult:
        """Perform all predecessor reads and durable writes away from GTK."""
        try:
            if request.operation == "probe":
                return _LegacyMigrationResult(request, found=legacy_migration.probe())
            if request.found is None:  # pragma: no cover - internal invariant
                return _LegacyMigrationResult(request, error="migration source is missing")
            if request.operation == "later":
                outcome = legacy_migration.postpone(source_dir=request.found.source)
                return _LegacyMigrationResult(request, outcome=outcome)
            if request.operation == "fresh":
                outcome = legacy_migration.decline(source_dir=request.found.source)
                return _LegacyMigrationResult(request, outcome=outcome)
            outcome = legacy_migration.migrate(source_dir=request.found.source)
            settings = config.load_strict()
            return _LegacyMigrationResult(request, outcome=outcome, settings=settings)
        except (config.ConfigError, legacy_migration.MigrationError) as error:
            return _LegacyMigrationResult(request, error=str(error))
        except Exception as error:  # pragma: no cover - defensive worker boundary
            detail = str(error) or error.__class__.__name__
            return _LegacyMigrationResult(request, error=f"migration worker failed: {detail}")

    def _finish_legacy_migration_job(
        self,
        request: _LegacyMigrationRequest,
        future: Future[_LegacyMigrationResult],
    ) -> bool:
        """Consume a migration result on GTK iff its window still exists."""
        if self._legacy_migration_future is not future:
            return GLib.SOURCE_REMOVE
        self._legacy_migration_future = None
        if request.operation == "probe" and request.window_generation == 0 and self._window is None:
            try:
                headless = future.result()
            except Exception:
                self._authoring_gate_state = "probe-failed"
                return GLib.SOURCE_REMOVE
            if (
                not headless.error
                and headless.found is not None
                and not headless.found.needs_decision
            ):
                self._open_authoring_after_migration()
            elif headless.error or headless.found is None:
                self._authoring_gate_state = "probe-failed"
            else:
                # A malformed/unsafe predecessor needs a visible user decision
                # before any profile mutation.
                self._authoring_gate_state = "decision"
            # A decision or probe error intentionally leaves the gate closed.
            # A later activation runs a fresh, visible probe with retry UI.
            return GLib.SOURCE_REMOVE
        current = (
            not self._legacy_migration_shutdown
            and request.generation == self._legacy_migration_generation
            and request.window_generation == self._window_generation
            and self._window is not None
        )
        if not current:
            if (
                not self._legacy_migration_shutdown
                and self._legacy_migration_retry_probe
                and self._window is not None
            ):
                self._legacy_migration_retry_probe = False
                self._legacy_migration_prompted = False
                self._close_legacy_migration_dialog()
                self._prompt_for_legacy_migration()
            return GLib.SOURCE_REMOVE

        self._close_legacy_migration_dialog()
        try:
            result = future.result()
        except Exception as error:  # pragma: no cover - executor boundary
            result = _LegacyMigrationResult(
                request,
                error=str(error) or error.__class__.__name__,
            )
        if result.error:
            self._finish_legacy_migration_failure(request, result.error)
            return GLib.SOURCE_REMOVE

        if request.operation == "probe":
            found = result.found
            if found is None:  # pragma: no cover - internal invariant
                self._finish_legacy_migration_failure(request, "migration probe returned no result")
            elif found.needs_decision:
                self._authoring_gate_state = "decision"
                self._present_legacy_migration_choice(found)
            else:
                self._open_authoring_after_migration()
                self._legacy_migration_deferred = False
                self._continue_first_activation()
            return GLib.SOURCE_REMOVE

        found = request.found
        outcome = result.outcome
        if found is None or outcome is None:  # pragma: no cover - internal invariant
            self._finish_legacy_migration_failure(request, "migration returned no result")
            return GLib.SOURCE_REMOVE
        if request.operation == "later":
            self._authoring_gate_state = "decision"
            self._legacy_migration_deferred = True
            self.window_report(outcome.detail + "; the choice will return next launch")
            return GLib.SOURCE_REMOVE
        if request.operation == "fresh":
            self._open_authoring_after_migration()
            self._legacy_migration_deferred = False
            self.window_report(outcome.detail)
            self._continue_first_activation()
            return GLib.SOURCE_REMOVE

        if result.settings is None:  # pragma: no cover - internal invariant
            self._finish_legacy_migration_failure(request, "imported settings were not loaded")
            return GLib.SOURCE_REMOVE
        # This callback is entered only by GLib.idle_add.  Session replacement,
        # window settings and palette rendering consequently stay GTK-owned.
        self._replace_session_after_migration(result.settings)
        self._open_authoring_after_migration()
        self._legacy_migration_deferred = False
        report = f" Report: {outcome.report}" if outcome.report is not None else ""
        self.window_report(outcome.detail + report)
        self._continue_first_activation()
        return GLib.SOURCE_REMOVE

    def _finish_legacy_migration_failure(
        self,
        request: _LegacyMigrationRequest,
        error: str,
    ) -> None:
        """Keep first-run setup blocked while offering a safe retry/deferral."""
        self._authoring_gate_state = "probe-failed" if request.operation == "probe" else "decision"
        if request.operation == "probe":
            self.window_report(f"Could not check older Wall-in-One data: {error}")
            self._present_legacy_probe_failure(error)
            return
        found = request.found
        if request.operation == "fresh":
            prefix = "Could not save the migration choice"
        elif request.operation == "later":
            prefix = "Could not postpone the migration choice"
        else:
            prefix = "Legacy import stopped without overwriting current data"
        self.window_report(f"{prefix}: {error}")
        if found is not None:
            self._present_legacy_migration_choice(found, failure=f"{prefix}: {error}")

    def _present_legacy_probe_failure(self, error: str) -> None:
        if self._window is None or self._legacy_migration_shutdown:
            return
        dialog = Adw.AlertDialog(
            heading="Could not check older data",
            body=(
                "Wall-in-One could not safely determine whether predecessor data needs a "
                f"decision. Current first-run setup remains paused.\n\n{error}"
            ),
        )
        dialog.add_response("later", "Not now")
        dialog.add_response("retry", "Try again")
        dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("retry")
        dialog.set_close_response("later")
        dialog.connect("response", self._on_legacy_probe_failure_response)
        self._legacy_migration_prompt = dialog
        dialog.present(self._window)

    def _on_legacy_probe_failure_response(
        self,
        dialog: Adw.AlertDialog,
        response: str,
    ) -> None:
        if self._legacy_migration_prompt is not dialog or self._legacy_migration_future is not None:
            return
        self._legacy_migration_prompt = None
        if response == "later":
            self._legacy_migration_deferred = True
            self.window_report("Older-data check postponed; the choice will return next launch")
            return
        if response == "retry":
            self._legacy_migration_prompted = False
            self._prompt_for_legacy_migration()

    def _on_legacy_migration_response(
        self,
        dialog: Adw.AlertDialog,
        response: str,
        found: legacy_migration.Probe,
    ) -> None:
        if self._legacy_migration_prompt is not dialog or self._legacy_migration_future is not None:
            return
        self._legacy_migration_prompt = None
        operation: _LegacyMigrationOperation
        if response == "later":
            operation = "later"
            heading = "Postponing older-data import"
            body = "Checking that the predecessor source is still the one you reviewed…"
        elif response == "fresh" and found.status != "in-progress":
            operation = "fresh"
            heading = "Saving your migration choice"
            body = "Recording the exact predecessor source without changing its files…"
        elif response == "import":
            operation = "import"
            heading = "Importing older data"
            body = (
                "Safely staging and verifying the current profile. "
                "The predecessor files will remain untouched…"
            )
        else:
            return
        self._show_legacy_migration_progress(heading, body)
        if not self._queue_legacy_migration_job(operation, found):
            self._close_legacy_migration_dialog()
            self._present_legacy_migration_choice(
                found,
                failure="The migration worker is unavailable; nothing changed.",
            )

    def _shutdown_legacy_migration_jobs(self, *, wait: bool = False) -> None:
        """Invalidate GTK delivery while allowing a started transaction to settle."""
        self._legacy_migration_shutdown = True
        self._legacy_migration_generation += 1
        self._legacy_migration_retry_probe = False
        self._close_legacy_migration_dialog()
        future = self._legacy_migration_future
        self._legacy_migration_future = None
        if future is not None:
            future.cancel()
        jobs = self._legacy_migration_jobs
        self._legacy_migration_jobs = None
        if jobs is not None:
            jobs.shutdown(wait=wait, cancel_futures=True)

    def _replace_session_after_migration(self, settings: config.Settings) -> None:
        """Reopen every split store which the just-completed import installed."""
        previous = self._session
        self._settings = settings
        self._settings_requested = settings
        self._session = Session(settings)
        previous.shutdown()
        if self._window is not None:
            self._window.apply_settings(settings)
        self.reload_palette()

    def _prompt_for_library_root(self) -> None:
        """Ask before treating any detected directory as our write target."""
        if (
            self._window is None
            or self._settings.roots
            or self._library_root_prompted
            or self._library_root_prompt is not None
        ):
            return
        self._library_root_prompted = True
        suggested = next(iter(scan.default_roots()), None)
        destination = (
            f"\n\nSuggested folder:\n{suggested}"
            if suggested is not None
            else "\n\nNo wallpaper folder was detected, so choose one manually."
        )
        dialog = Adw.AlertDialog(
            heading="Choose a library folder",
            body=(
                "Wall-in-One has not been configured with a wallpaper folder. "
                "It will not download wallpapers or generate stills until you "
                f"choose where those files belong.{destination}"
            ),
        )
        dialog.add_response("later", "Not now")
        dialog.add_response("manual", "Choose folder manually")
        if suggested is not None:
            # Keep a long filesystem path in the selectable body rather than
            # turning it into an enormous button label.
            dialog.add_response("default", "Use default")
            dialog.set_response_appearance("default", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("default")
        else:
            dialog.set_response_appearance("manual", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("manual")
        dialog.set_close_response("later")
        dialog.connect("response", self._on_library_root_response, suggested)
        self._library_root_prompt = dialog
        dialog.present(self._window)

    def _on_library_root_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        suggested: Path | None,
    ) -> None:
        self._library_root_prompt = None
        if response == "default" and suggested is not None:
            self._save_initial_library_root(suggested)
        elif response == "manual":
            self._choose_initial_library_root()

    def _choose_initial_library_root(self) -> None:
        if self._window is None:
            return
        chooser = Gtk.FileDialog(title="Choose your wallpaper folder", modal=True)
        chooser.select_folder(self._window, None, self._on_initial_library_root_chosen)

    def _on_initial_library_root_chosen(
        self,
        chooser: Gtk.FileDialog,
        result: Gio.AsyncResult,
    ) -> None:
        try:
            chosen = chooser.select_folder_finish(result)
        except GLib.Error:
            return
        raw = chosen.get_path() if chosen is not None else None
        if raw is None:
            self.window_report("That folder is not on this machine's filesystem")
            return
        self._save_initial_library_root(Path(raw))

    def _save_initial_library_root(self, root: Path) -> None:
        """Persist consent before the scan/download/still paths can use it."""
        self.update_settings_async(
            roots=(root,),
            on_success=lambda _settings: self.window_report(
                f"Using {root} as the wallpaper library"
            ),
            on_error=lambda error: self.window_report(
                f"Library folder was not saved; nothing changed: {error}"
            ),
        )

    def do_shutdown(self) -> None:
        self._stop_palette_monitor()
        self._shutdown_theme_jobs()
        self._shutdown_legacy_migration_jobs()
        self._stop_cycle()
        if self._schedule_source:
            GLib.source_remove(self._schedule_source)
            self._schedule_source = 0
        self._stop_runtime_status_timer()
        with self._runtime_config_lock:
            self._runtime_shutdown = True
            self._runtime_authoring_generation += 1
            self._runtime_health_request = None
            self._runtime_library_ready.set()
        self._runtime_cancellation.cancel()
        self._window_generation += 1
        self._shutdown_library_scan_jobs()
        self._shutdown_authoring_jobs()
        if self._runtime_jobs is not None:
            # The application cancellation group already closed every active
            # socket and refuses a race-late registration. Invalidate queued
            # work too; no completion may reach the destroyed window.
            self._runtime_jobs.shutdown(wait=False, cancel_futures=True)
            self._runtime_jobs = None
        self._stills.shutdown()
        self._session.shutdown()
        with self._browse_lock:
            self._browse_shutdown = True
            browse_transports = tuple(self._browse_transports)
            self._browse_transports.clear()
        for browser in browse_transports:
            browser.shutdown()
        if self._browse_jobs is not None:
            # Active transports were closed above, waking their socket reads;
            # queued futures are invalidated here. Downloads stage under a
            # temporary name and only publish after validation.
            self._browse_jobs.shutdown(wait=False, cancel_futures=True)
            self._browse_jobs = None
        if self._control is not None:
            self._control.stop()
            self._control = None
        self._held = False
        Adw.Application.do_shutdown(self)

    def _on_close_request(self, window: Gtk.Window) -> bool:
        # Every settings edit is persisted by ``update_settings``.  Saving the
        # in-memory recovery defaults here used to overwrite a malformed file,
        # and even a valid future document lost unknown keys merely because an
        # older app was opened and closed without an edit.
        if self._window is window:
            # The default handler destroys the window after this callback.
            # Drop our reference now so a later activation builds a fresh one
            # instead of trying to present a destroyed GTK object.
            self._window = None
            self._window_generation += 1
            self._legacy_migration_generation += 1
            if self._legacy_migration_future is not None:
                self._legacy_migration_retry_probe = True
            self._legacy_migration_prompt = None
            if not self._legacy_migration_deferred:
                self._legacy_migration_prompted = False
            self._stop_runtime_status_timer()
        return False

    def request_quit(self) -> None:
        """Stop deliberately, after every already-accepted durable tail."""
        self._quit_requested = True
        self._maybe_finish_requested_quit()

    @property
    def legacy_service(self) -> bool:
        """Whether this Python process intentionally owns compatibility timers."""
        return self._service_start

    @property
    def runtime_status(self) -> dict[str, object] | None:
        """Last valid atomic Rust status, retained across transient timeouts."""
        return self._runtime_status

    def present_page(self, page: str) -> None:
        """Present the singleton window with one primary workflow page visible."""
        # In service mode there is no window yet. Activating this same
        # GApplication constructs one locally; an ordinary second invocation
        # is forwarded here by Gio for the same reason.
        self.activate()
        if self._window is None:  # pragma: no cover - a broken GTK invariant
            raise RuntimeError("Wall-in-One could not create its window")
        self._window.show_page(page)

    # -- palette ---------------------------------------------------------

    def _start_palette_monitor(self) -> None:
        """Watch the palette's directory so atomic replacement stays visible.

        Noctalia renders through a temporary file and rename. Watching the
        palette inode itself would therefore work once and then go deaf.
        """
        palette_path = paths.palette_path()
        paths.ensure_directory(palette_path.parent)
        directory = Gio.File.new_for_path(str(palette_path.parent))
        try:
            monitor = directory.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error as error:
            # The post-hook remains a complete fast path, so an unavailable
            # monitor should cost redundancy rather than application startup.
            print(f"warning: palette monitor unavailable: {error}", file=sys.stderr)
        else:
            monitor.connect("changed", self._on_palette_directory_changed)
            self._palette_monitor = monitor
        # Missing/disabled template output cannot notify us. Noctalia writes
        # scheme and mode changes to settings, so watch that independent event
        # source too, without introducing a periodic shell-IPC polling loop.
        self._start_noctalia_settings_monitor()

    def _start_noctalia_settings_monitor(self) -> None:
        if self._noctalia_settings_monitor is not None:
            self._noctalia_settings_monitor.cancel()
            self._noctalia_settings_monitor = None
        # A fresh install may start before Noctalia has created its state
        # directory. Watch the nearest existing ancestor, then move the watch
        # down as directories appear; never create Noctalia's directories.
        parent = paths.noctalia_settings_path().parent
        while not parent.is_dir() and parent != parent.parent:
            parent = parent.parent
        settings_directory = Gio.File.new_for_path(str(parent))
        try:
            settings_monitor = settings_directory.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        settings_monitor.connect("changed", self._on_palette_directory_changed)
        self._noctalia_settings_monitor = settings_monitor

    def _stop_palette_monitor(self) -> None:
        if self._palette_reload_source:
            GLib.source_remove(self._palette_reload_source)
            self._palette_reload_source = 0
        if self._palette_monitor is not None:
            self._palette_monitor.cancel()
            self._palette_monitor = None
        if self._noctalia_settings_monitor is not None:
            self._noctalia_settings_monitor.cancel()
            self._noctalia_settings_monitor = None

    def _on_palette_directory_changed(
        self,
        _monitor: Gio.FileMonitor,
        changed: Gio.File,
        other: Gio.File | None,
        _event_type: Gio.FileMonitorEvent,
    ) -> None:
        changed_paths = {changed.get_path(), other.get_path() if other is not None else None}
        settings_path = paths.noctalia_settings_path()
        if {str(parent) for parent in settings_path.parents}.intersection(changed_paths):
            self._start_noctalia_settings_monitor()
        elif not {str(paths.palette_path()), str(settings_path)}.intersection(changed_paths):
            return
        # One atomic render can emit created, moved and changes-done events.
        # Restarting a short trailing timeout turns that burst into one repaint.
        if self._palette_reload_source:
            GLib.source_remove(self._palette_reload_source)
        self._palette_reload_source = GLib.timeout_add(
            PALETTE_RELOAD_DEBOUNCE_MS, self._reload_monitored_palette
        )

    def _reload_monitored_palette(self) -> bool:
        self._palette_reload_source = 0
        self.reload_palette()
        return GLib.SOURCE_REMOVE

    def reload_palette(
        self,
        on_complete: PaletteReloadCallback | None = None,
    ) -> source.ResolvedPalette:
        """Queue a live palette resolution without blocking GTK.

        Noctalia runs our template's post-hook on every successful render, not
        only when the colours actually changed.  Those requests and file
        monitor events therefore coalesce to one latest settings snapshot.
        The return value is deliberately the last usable palette (or the cheap
        fixed bootstrap palette), not a promise that shell I/O already
        finished; callers which need completion use ``on_complete``.
        """
        if self._palette_reload_source:
            # The post-hook normally reaches us before the monitor debounce.
            # Let that lower-latency path replace the queued repaint rather
            # than applying the same rendered palette twice.
            GLib.source_remove(self._palette_reload_source)
            self._palette_reload_source = 0

        if self._resolved is None:
            # A neutral app-owned stylesheet makes first activation usable
            # immediately.  ``fixed`` is pure in-process work; only live
            # resolution belongs on the worker.
            self._resolved = source.fixed()
            self._apply_stylesheet(self._resolved)
            if self._window is not None:
                self._window.show_palette(self._resolved)

        start_worker = False
        rejected = False
        with self._theme_lock:
            if self._theme_shutdown:
                rejected = True
            else:
                self._theme_generation += 1
                self._theme_pending_palette = _PaletteRequest(
                    generation=self._theme_generation,
                    follow_noctalia=self._settings.follow_noctalia_palette,
                    scheme=self._settings.preview_scheme,
                )
                if on_complete is not None:
                    self._theme_reload_callbacks.append(on_complete)
                if not self._theme_draining:
                    self._theme_draining = True
                    start_worker = True
        if rejected:
            if on_complete is not None:
                GLib.idle_add(
                    self._deliver_rejected_palette_callback,
                    on_complete,
                )
        elif start_worker:
            self._theme_pool().submit(self._drain_theme_jobs)
        return self._resolved

    @staticmethod
    def _deliver_rejected_palette_callback(callback: PaletteReloadCallback) -> bool:
        callback(None, "application is shutting down")
        return GLib.SOURCE_REMOVE

    def _theme_pool(self) -> ThreadPoolExecutor:
        """The sole worker for live theme IPC and palette resolution.

        Scheme-preview generators intentionally retain their own already
        bounded pools: ten speculative previews must not sit ahead of a user's
        explicit Apply gesture.  This lane owns only live application colour
        state, and one worker gives those mutations a total order.
        """
        if self._theme_jobs is None:
            self._theme_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="theme-io")
        return self._theme_jobs

    def _drain_theme_jobs(self) -> None:
        """Run explicit choices first, then the newest coalesced resolution."""
        while True:
            with self._theme_lock:
                if self._theme_shutdown:
                    self._theme_draining = False
                    return
                action = self._theme_actions.popleft() if self._theme_actions else None
                wallpaper_scan = None
                request = None
                if action is None:
                    wallpaper_scan = self._theme_pending_wallpaper_scan
                    self._theme_pending_wallpaper_scan = None
                if action is None and wallpaper_scan is None:
                    request = self._theme_pending_palette
                    self._theme_pending_palette = None
                if action is None and wallpaper_scan is None and request is None:
                    self._theme_draining = False
                    return

            if action is not None:
                error = ""
                try:
                    noctalia.message(
                        "color-scheme-set",
                        action.palette_source,
                        action.name,
                        cancelled=self._theme_cancelled.is_set,
                    )
                except Exception as caught:
                    # The worker boundary must turn even an unexpected wrapper
                    # bug into a visible failure, never an unobserved Future.
                    error = str(caught) or caught.__class__.__name__
                GLib.idle_add(
                    self._finish_theme_action,
                    _ThemeActionResult(action.generation, error),
                )
                continue

            if wallpaper_scan is not None:
                try:
                    active = noctalia.current_wallpaper(cancelled=self._theme_cancelled.is_set)
                    wallpaper_result = _WallpaperQueryResult(wallpaper_scan, active)
                except noctalia.NoctaliaError:
                    # Noctalia is optional; its absence merely leaves the
                    # rebuilt playlist at its deterministic first entry.
                    wallpaper_result = _WallpaperQueryResult(wallpaper_scan, None)
                except Exception as caught:
                    wallpaper_result = _WallpaperQueryResult(
                        wallpaper_scan,
                        None,
                        str(caught) or caught.__class__.__name__,
                    )
                GLib.idle_add(self._finish_wallpaper_query, wallpaper_result)
                continue

            assert request is not None
            try:
                resolved = (
                    source.resolve(
                        scheme=request.scheme,
                        cancelled=self._theme_cancelled.is_set,
                    )
                    if request.follow_noctalia
                    else source.fixed()
                )
                result = _PaletteResult(request.generation, resolved)
            except Exception as caught:
                result = _PaletteResult(
                    request.generation,
                    None,
                    str(caught) or caught.__class__.__name__,
                )
            GLib.idle_add(self._finish_palette_resolution, result)

    def _finish_theme_action(self, result: _ThemeActionResult) -> bool:
        """Deliver only the latest explicit selection on GTK's thread."""
        with self._theme_lock:
            if self._theme_shutdown:
                return GLib.SOURCE_REMOVE
            callback = self._theme_action_callbacks.pop(result.generation, None)
            current = result.generation == self._theme_action_generation
            # Older callbacks retain dialog/widget closures for no useful
            # reason once a later choice supersedes their visible result.
            if current:
                self._theme_action_callbacks.clear()
        if current and callback is not None:
            callback(result.error)
        elif current and result.error:
            self.window_report(f"Noctalia palette change failed: {result.error}")
        return GLib.SOURCE_REMOVE

    def _queue_wallpaper_query(self, scan_generation: int) -> None:
        """Coalesce live-wallpaper queries behind accepted GUI scans."""
        start_worker = False
        with self._theme_lock:
            if self._theme_shutdown:
                return
            self._theme_pending_wallpaper_scan = scan_generation
            if not self._theme_draining:
                self._theme_draining = True
                start_worker = True
        if start_worker:
            self._theme_pool().submit(self._drain_theme_jobs)

    def _finish_wallpaper_query(self, result: _WallpaperQueryResult) -> bool:
        """Adopt a queried path only into the scan generation which asked."""
        if (
            self._theme_shutdown
            or self._library_scan_shutdown
            or result.scan_generation != self._library_scan_generation
        ):
            return GLib.SOURCE_REMOVE
        if result.error:
            self.window_report(f"Could not read Noctalia's active wallpaper: {result.error}")
            return GLib.SOURCE_REMOVE
        if self._session.sync_to_wallpaper(result.active) and self._window is not None:
            self._window.show_current(self._session)
        return GLib.SOURCE_REMOVE

    def _finish_palette_resolution(self, result: _PaletteResult) -> bool:
        """Adopt one immutable worker answer iff no newer request superseded it."""
        with self._theme_lock:
            if self._theme_shutdown or result.generation != self._theme_generation:
                return GLib.SOURCE_REMOVE
            callbacks = tuple(self._theme_reload_callbacks)
            self._theme_reload_callbacks.clear()

        resolved = result.resolved
        error = result.error
        if resolved is not None:
            try:
                # Prepare/apply before replacing the authoritative snapshot.
                # A bad palette must not strand the reload's callbacks or
                # poison the colors used when another window opens.
                self._apply_stylesheet(resolved)
            except Exception as caught:
                resolved = None
                error = str(caught) or caught.__class__.__name__
        if resolved is None:
            detail = error or "unknown palette resolution failure"
            self.window_report(f"Palette reload failed; keeping current colours: {detail}")
        else:
            self._resolved = resolved
            if self._window is not None:
                self._window.show_palette(resolved)
        for callback in callbacks:
            callback(resolved, error)
        return GLib.SOURCE_REMOVE

    def apply_noctalia_palette_async(
        self,
        palette_source: str,
        name: str,
        *,
        on_complete: ThemeActionCallback | None = None,
    ) -> bool:
        """Queue an explicit Noctalia selection ahead of pending repaints.

        Repeated clicks retain the action already executing and only the newest
        queued choice.  The final selection is therefore deterministic without
        building an unbounded ten-second-IPC backlog.  Its matching resolution
        snapshot is installed atomically with the action under the scheduler
        lock, so it can never overtake the selection it is meant to observe.
        """
        start_worker = False
        with self._theme_lock:
            if self._theme_shutdown:
                if on_complete is not None:
                    GLib.idle_add(self._deliver_rejected_theme_callback, on_complete)
                return False
            self._theme_generation += 1
            self._theme_action_generation += 1
            action = _ThemeAction(
                generation=self._theme_action_generation,
                palette_source=palette_source,
                name=name,
            )
            # One action may already be executing outside this lock.  Anything
            # still in the deque has not started and is safely superseded by
            # the user's newest selection.
            self._theme_actions.clear()
            self._theme_actions.append(action)
            self._theme_action_callbacks.clear()
            if on_complete is not None:
                self._theme_action_callbacks[action.generation] = on_complete
            self._theme_pending_palette = _PaletteRequest(
                generation=self._theme_generation,
                follow_noctalia=self._settings.follow_noctalia_palette,
                scheme=self._settings.preview_scheme,
            )
            if not self._theme_draining:
                self._theme_draining = True
                start_worker = True
        if start_worker:
            self._theme_pool().submit(self._drain_theme_jobs)
        return True

    @staticmethod
    def _deliver_rejected_theme_callback(callback: ThemeActionCallback) -> bool:
        callback("application is shutting down")
        return GLib.SOURCE_REMOVE

    def use_preview_scheme(
        self,
        scheme: str,
        *,
        sync_noctalia: bool,
        on_complete: ThemeActionCallback | None = None,
    ) -> bool:
        """Persist a preview scheme, optionally ordering its shell sync first."""

        def saved(_settings: config.Settings) -> None:
            if sync_noctalia:
                self.apply_noctalia_palette_async(
                    "wallpaper",
                    scheme,
                    on_complete=on_complete,
                )
            else:
                self.reload_palette()
                if on_complete is not None:
                    on_complete("")

        def failed(error: str) -> None:
            if on_complete is not None:
                on_complete(error)
            else:
                self.window_report(f"Scheme preference was not saved; nothing changed: {error}")

        return self.update_settings_async(
            preview_scheme=scheme,
            on_success=saved,
            on_error=failed,
        )

    def _shutdown_theme_jobs(self, *, wait: bool = False) -> None:
        """Invalidate all late deliveries and stop accepting live-theme work."""
        with self._theme_lock:
            self._theme_shutdown = True
            self._theme_cancelled.set()
            self._theme_generation += 1
            self._theme_action_generation += 1
            self._theme_actions.clear()
            self._theme_pending_wallpaper_scan = None
            self._theme_pending_palette = None
            self._theme_reload_callbacks.clear()
            self._theme_action_callbacks.clear()
        # ThreadPoolExecutor threads are joined at interpreter exit. Kill the
        # app-owned CLI process group after invalidating every delivery so a
        # wedged 10/30-second Noctalia call cannot keep the GUI process alive.
        noctalia.cancel_pending()
        jobs = self._theme_jobs
        self._theme_jobs = None
        if jobs is not None:
            jobs.shutdown(wait=wait, cancel_futures=True)

    def _apply_stylesheet(self, resolved: source.ResolvedPalette) -> None:
        stylesheet = css.render(resolved.palette, opacity=self._settings.opacity)
        self._provider.load_from_string(stylesheet)
        # Match the palette's own mode so libadwaita's built-in rules agree
        # with the colours we just handed it. This also settles the argument
        # with GtkSettings:gtk-application-prefer-dark-theme, which Noctalia's
        # own gtk4 template writes into ~/.config/gtk-4.0/settings.ini and
        # which libadwaita warns about on startup; the style manager wins.
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(
            Adw.ColorScheme.FORCE_DARK
            if resolved.palette.mode == "dark"
            else Adw.ColorScheme.FORCE_LIGHT
        )

    def _on_settings_changed(self, settings: config.Settings) -> None:
        library_sources_changed = (settings.roots, settings.scan_workshop) != (
            self._settings.roots,
            self._settings.scan_workshop,
        )
        palette_source_changed = (
            settings.preview_scheme,
            settings.follow_noctalia_palette,
        ) != (
            self._settings.preview_scheme,
            self._settings.follow_noctalia_palette,
        )
        self._settings = settings
        async_scan = library_sources_changed and self._window is not None
        self._session.update_settings(settings, rescan_library=not async_scan)
        if self._window is not None:
            # The settings document can also move without a Preferences-page
            # gesture. Keep those persistent widgets on the application-owned
            # snapshot so their next edit cannot write stale sibling fields.
            self._window.apply_settings(settings)
        if async_scan:
            self.refresh_library()
        else:
            self._publish_runtime_for_context()
        if palette_source_changed:
            self.reload_palette()
        elif self._resolved is not None:
            self._apply_stylesheet(self._resolved)

    @property
    def resolved_palette(self) -> source.ResolvedPalette | None:
        return self._resolved

    def open_palette_browser(self) -> None:
        """Open the palette browser, from wherever asked."""
        if self._window is not None:
            self._window.open_palette_browser()

    def window_report(self, message: str) -> None:
        """Put a management-page result in the window's toast overlay."""
        if self._window is not None:
            self._window.report(message)

    # -- library ---------------------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    def refresh_library(self) -> None:
        """Rescan and reconcile without filesystem work on GTK's thread.

        Headless callers keep the synchronous :class:`Session` contract. With
        a window present, one generation-tagged worker replays the removal
        journal, walks the library, and performs confirmed Workshop cleanup
        through detached Stores.  The existing grid, search, scroll and focus
        remain in place. A newer request cooperatively cancels the older one
        and makes any already-running result ineligible to land.
        """
        # An idle refresh can already be queued when the final window and
        # workers shut down. Window absence is not permission to re-enter the
        # synchronous headless path on a closed Session.
        if self._runtime_shutdown or self._library_scan_shutdown:
            return
        if self._removal_active:
            self._refresh_after_removal = True
            return
        if self._window is None:
            with self._runtime_config_lock:
                runtime_generation = self._runtime_library_scan_generation
                runtime_pending = self._runtime_library_scan_pending
            if self._removal_convergence_held:
                self._removal_convergence_scan_generation = runtime_generation
            try:
                self._session.refresh()
            except Exception as error:
                detail = str(error) or error.__class__.__name__
                if runtime_pending:
                    self._finish_runtime_library_scan(runtime_generation, error=detail)
                else:
                    self._mark_removal_scan_terminal(runtime_generation)
                self.window_report(f"Library scan failed: {detail}")
                return
            if runtime_pending:
                self._adopt_runtime_library_scan(runtime_generation)
            self._finish_library_refresh()
            if runtime_pending:
                self._finish_runtime_library_scan(runtime_generation)
            return

        try:
            request = self._session.prepare_library_refresh()
        except LibraryRefreshBusyError as error:
            self.window_report(f"Library refresh will wait: {error}")
            return
        self._library_scan_generation += 1
        generation = self._library_scan_generation
        if self._removal_convergence_held:
            self._removal_convergence_scan_generation = generation
        previous_request = self._library_scan_request
        if previous_request is not None:
            previous_request.cancel()
        previous = self._library_scan_future
        if previous is not None:
            previous.cancel()
        self._window.show_library_scanning(True)
        self._begin_runtime_library_scan(generation)

        try:
            future = self._library_scan_pool().submit(request.scan)
        except Exception as error:
            detail = str(error) or error.__class__.__name__
            request.cancel()
            self._window.show_library_scanning(False)
            self.window_report(f"Library scan could not start: {detail}")
            self._finish_runtime_library_scan(generation, error=detail)
            return
        self._library_scan_request = request
        self._library_scan_future = future
        future.add_done_callback(lambda done: self._post_library_scan(generation, request, done))

    def _library_scan_pool(self) -> ThreadPoolExecutor:
        """The single I/O lane used by graphical library scans."""
        if self._library_scan_jobs is None:
            self._library_scan_jobs = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="library-scan",
            )
        return self._library_scan_jobs

    def _shutdown_library_scan_jobs(self, *, wait: bool = False) -> None:
        """Cancel queued/coop work and invalidate every late GTK delivery."""
        self._library_scan_shutdown = True
        self._library_scan_generation += 1
        release_lifetime = False
        with self._runtime_config_lock:
            if self._runtime_library_scan_held:
                self._runtime_library_scan_held = False
                self._runtime_library_scan_hold_generation = 0
                release_lifetime = True
            self._runtime_library_scan_pending = False
            self._runtime_library_ready.set()
        request = self._library_scan_request
        self._library_scan_request = None
        if request is not None:
            request.cancel()
        jobs = self._library_scan_jobs
        self._library_scan_jobs = None
        if jobs is not None:
            jobs.shutdown(wait=wait, cancel_futures=True)
        self._library_scan_future = None
        if release_lifetime:
            self.release()
            if not self._runtime_shutdown:
                self._maybe_finish_requested_quit()

    def _post_library_scan(
        self,
        generation: int,
        request: LibraryRefreshPlan,
        future: Future[Library],
    ) -> None:
        """Queue lifecycle reconciliation after one mutation-free media walk."""

        def deliver() -> bool:
            if self._library_scan_shutdown:
                return GLib.SOURCE_REMOVE
            if generation != self._library_scan_generation:
                # No cleanup has happened yet: the scan phase is deliberately
                # read-only, so a superseded result can simply disappear.
                return GLib.SOURCE_REMOVE
            self._library_scan_future = None
            self._library_scan_request = None
            try:
                library = future.result()
            except LibraryRefreshCancelledError as error:
                detail = str(error)
                if self._window is not None:
                    self._window.show_library_scanning(False)
                self._finish_runtime_library_scan(generation, error=detail)
                return GLib.SOURCE_REMOVE
            except Exception as error:  # defensive boundary around injected scanners too
                detail = str(error) or error.__class__.__name__
                if self._window is not None:
                    self._window.show_library_scanning(False)
                    self._window.report(f"Library scan failed: {detail}")
                self._finish_runtime_library_scan(generation, error=detail)
                return GLib.SOURCE_REMOVE
            self._queue_library_reconciliation(generation, request, library)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    def _queue_library_reconciliation(
        self,
        generation: int,
        request: LibraryRefreshPlan,
        library: Library,
    ) -> None:
        """Serialize deletion replay and Workshop cleanup with all authoring."""

        def finished(result: LibraryRefreshResult) -> None:
            removed_paths = {
                *(cleanup.item.path for cleanup in result.cleanups),
                *(item.path for item in result.removed_workshop),
            }
            self._removed_item_paths.update(removed_paths)
            if generation != self._library_scan_generation:
                # The cleanup is durable even when a newer scan won. Mirror
                # only semantic deletion facts, never the stale Library.
                self._session.adopt_library_cleanups(result.cleanups)
                self._session.adopt_library_fault_repairs(result.repaired_faults)
                return
            try:
                self._session.adopt_library_refresh(result)
                # A later scan which actually sees the item again is the clean
                # reinstall boundary and may release its process-local taboo.
                self._removed_item_paths.difference_update(
                    item.path for item in result.library.items
                )
                self._adopt_runtime_library_scan(generation)
                if result.replay_failures:
                    self.window_report(
                        "Pending removal cleanup could not finish: "
                        + "; ".join(result.replay_failures[:3])
                        + ". Fix the state-directory problem and refresh to retry."
                    )
                self._finish_library_refresh(scan_generation=generation)
            except Exception as error:  # defensive GTK delivery boundary
                self._library_reconciliation_failed(generation, str(error))
                return
            self._finish_runtime_library_scan(generation)

        self.authoring_action_async(
            lambda: request.reconcile(library),
            finished,
            failure=lambda message: self._library_reconciliation_failed(generation, message),
            allow_during_quit=True,
        )

    def _library_reconciliation_failed(self, generation: int, message: str) -> None:
        """Release one scan barrier after its ordered cleanup could not run."""
        if generation != self._library_scan_generation:
            return
        detail = message or "unknown lifecycle reconciliation failure"
        if self._window is not None:
            self._window.show_library_scanning(False)
            self._window.report(f"Library result could not be installed: {detail}")
        self._finish_runtime_library_scan(generation, error=detail)

    def _finish_library_refresh(self, *, scan_generation: int | None = None) -> None:
        """Reconcile and render one installed scan, always on the main thread."""
        # A confirmed Workshop uninstall clears authored metadata, not an
        # unpinned generated still. Let a reinstall at the same path enter the
        # still-maker again instead of inheriting the process-local "already
        # attempted" memo.
        for item in self._session.removed_workshop:
            self._stills.forget(item.path)
        if self._session.workshop_cleanup_failures:
            self.window_report(
                "Workshop uninstall"
                + manage.metadata_cleanup_note(self._session.workshop_cleanup_failures)
            )
        if self._window is None:
            # The explicit compatibility service is headless and has no GTK
            # responsiveness contract. Keep its simple synchronous startup.
            self._session.sync_with_noctalia()
        else:
            self._queue_wallpaper_query(
                self._library_scan_generation if scan_generation is None else scan_generation
            )
        self._publish_runtime_for_context()
        if self._window is not None:
            self._window.show_library_scanning(False)
            self._window.show_library(self._session)
            self.refresh_runtime_status_async()
        self._make_missing_stills()

    def _begin_runtime_library_scan(self, generation: int) -> None:
        """Make a source-changing scan an explicit publication barrier."""
        sources = (self._settings.roots, self._settings.scan_workshop)
        acquire_lifetime = False
        with self._runtime_config_lock:
            source_barrier = (
                self._accepted_library_sources != sources or self._runtime_library_scan_held
            )
            self._runtime_library_scan_generation = generation
            self._runtime_library_scan_sources = sources
            self._runtime_library_scan_pending = True
            self._runtime_library_error = ""
            if source_barrier:
                self._runtime_library_scan_hold_generation = generation
                acquire_lifetime = not self._runtime_library_scan_held
                self._runtime_library_scan_held = True
            if self._accepted_library_sources != sources:
                self._runtime_library_ready.clear()
        if acquire_lifetime:
            self.hold()

    def _adopt_runtime_library_scan(self, generation: int) -> None:
        """Tag an accepted Library before its publication request is captured."""
        with self._runtime_config_lock:
            if generation != self._runtime_library_scan_generation:
                return
            self._accepted_library_sources = self._runtime_library_scan_sources

    def _finish_runtime_library_scan(self, generation: int, *, error: str = "") -> None:
        """Release actions waiting for the current source-changing scan."""
        release_lifetime = False
        with self._runtime_config_lock:
            if generation != self._runtime_library_scan_generation:
                return
            self._runtime_library_scan_pending = False
            self._runtime_library_error = error
            self._runtime_library_ready.set()
            release_lifetime = (
                self._runtime_library_scan_held
                and generation == self._runtime_library_scan_hold_generation
            )
            if release_lifetime:
                self._runtime_library_scan_held = False
                self._runtime_library_scan_hold_generation = 0
        self._mark_removal_scan_terminal(generation)
        # A compiler may have durably written the requested document and then
        # deferred its reload at this scan barrier.  Run the terminal pass now
        # even when no newer generation needs compilation.
        self._ensure_runtime_compile_queued()
        # Successful adoption queued its runtime publication before reaching
        # this callback. That publication owns a separate generation hold;
        # failures have no new runtime truth and are terminal here.
        if release_lifetime:
            self.release()
            self._maybe_finish_requested_quit()

    def _mark_removal_scan_terminal(self, generation: int) -> None:
        """Record the bounded post-delete scan tail, including an honest error."""
        if not self._removal_convergence_held:
            return
        expected = self._removal_convergence_scan_generation
        if expected is None or generation != expected:
            return
        self._removal_convergence_scan_terminal = True
        self._maybe_finish_removal_convergence()

    def _update_runtime_document(self) -> tuple[bool, bool]:
        """Synchronously write a live Session for explicit headless callers."""
        try:
            changed = runtime_config.update(self._settings, self._session)
        except runtime_config.RuntimeConfigError as error:
            self.window_report(str(error))
            return False, False
        if changed:
            with self._runtime_config_lock:
                self._runtime_document_generation += 1
        return True, changed

    def _publish_runtime(self) -> bool:
        """Synchronously publish authoring state for non-GUI callers.

        ``True`` means the generated document is usable, including when no
        runtime is currently listening.  It does not mean a service exists.
        GUI callbacks use :meth:`_publish_runtime_async` so a socket timeout
        never stalls GTK.
        """
        valid, changed = self._update_runtime_document()
        if not valid:
            return False
        if not changed:
            return True
        try:
            response = client.send_runtime("reload")
        except client.NotRunningError:
            if self._window is not None:
                self._window.show_runtime_unavailable()
            return True
        except client.ControlError as error:
            self.window_report(f"Runtime reload failed: {error}")
            return False
        if not response.ok:
            self.window_report(f"Runtime rejected the new configuration: {response.message}")
            return False
        with self._runtime_config_lock:
            self._runtime_loaded_generation = self._runtime_document_generation
        return True

    def _publish_runtime_async(self) -> bool:
        """Queue a durable authoring compilation without doing I/O on GTK.

        Capturing the accepted :class:`Library` is constant-time.  The worker
        reloads Settings and every JSON store from disk under the compiler
        lock, compiles and fsyncs the document, then reloads Rust in that same
        ordered lane.  A burst replaces the pending request rather than
        copying or rendering each intermediate authoring state.
        """
        if self._runtime_shutdown:
            return False
        with self._runtime_config_lock:
            self._runtime_authoring_generation += 1
            library_sources = self._accepted_library_sources
            if library_sources is None:
                # Before the first real scan, the Library roots themselves are
                # the only accepted source evidence. A configured root which
                # has not landed yet therefore cannot masquerade as current.
                library_sources = (
                    self._session.library.roots,
                    self._settings.scan_workshop,
                )
            self._runtime_authoring_request = _RuntimeAuthoringRequest(
                self._runtime_authoring_generation,
                self._session.library,
                library_sources,
            )
            self._runtime_publication_hold_target = self._runtime_authoring_generation
            acquire_lifetime = not self._runtime_publication_held
            self._runtime_publication_held = True
        if acquire_lifetime:
            self.hold()
        self._ensure_runtime_compile_queued()
        return True

    def _publish_runtime_for_context(self) -> bool:
        """Use the non-blocking path whenever a real window owns this call."""
        if self._runtime_shutdown:
            return False
        if self._window is not None:
            published = self._publish_runtime_async()
            if published and self._removal_convergence_held:
                with self._runtime_config_lock:
                    request = self._runtime_authoring_request
                    if request is not None:
                        self._removal_convergence_runtime_target = max(
                            self._removal_convergence_runtime_target,
                            request.generation,
                        )
                        self._removal_convergence_runtime_terminal = (
                            self._runtime_compiled_generation
                            >= self._removal_convergence_runtime_target
                        )
                self._maybe_finish_removal_convergence()
            return published
        published = self._publish_runtime()
        if self._removal_convergence_held:
            self._removal_convergence_runtime_terminal = published
            self._maybe_finish_removal_convergence()
        return published

    def _runtime_pool(self) -> ThreadPoolExecutor:
        """The ordered, single-flight lane for every GUI runtime request."""
        if self._runtime_jobs is None:
            self._runtime_jobs = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="runtime-socket",
            )
        return self._runtime_jobs

    def _authoring_pool(self) -> ThreadPoolExecutor:
        """The ordered lane for every GUI/control state-file transaction."""
        if self._authoring_jobs is None:
            self._authoring_jobs = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="control-authoring",
            )
        return self._authoring_jobs

    @property
    def authoring_ready(self) -> bool:
        """Whether current-profile writes are safe with respect to migration."""
        return (
            self._authoring_migration_ready
            and not self._authoring_shutdown
            and not self._quit_requested
        )

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        """Refuse a GUI mutation until the predecessor decision is settled."""
        if self.authoring_ready:
            return True
        if report:
            self.window_report(self._migration_blocked_response().message)
        return False

    def _migration_blocked_response(self) -> Response:
        if self._authoring_gate_state == "checking":
            return Response.failure(
                "current-profile authoring is still opening; retry shortly",
                kind="authoring-busy",
            )
        if self._authoring_gate_state == "repair":
            return Response.failure(
                "current authoring needs repair before changes can be saved; resolve the "
                "reported dangling playlist references and retry",
                kind="authoring-repair-required",
            )
        if self._authoring_gate_state == "probe-failed":
            return Response.failure(
                "older Wall-in-One data could not be checked; current-profile authoring is "
                "paused (open the app and choose Try again or Not now)",
                kind="migration-decision-required",
            )
        return Response.failure(
            "older Wall-in-One data still needs a decision; current-profile authoring is "
            "paused (open the app and choose Import safely or Start fresh)",
            kind="migration-decision-required",
        )

    def _shutdown_authoring_jobs(self) -> None:
        """Invalidate queued authoring and suppress every late socket reply."""
        self._authoring_shutdown = True
        queued = tuple(self._authoring_queue)
        self._authoring_queue.clear()
        for task in queued:
            self._finalize_authoring_task(task)
            self._release_authoring_lifetime(task)
        if self._authoring_jobs is not None:
            # A running atomic store operation owns its own bounded lock/write
            # transaction and must be allowed to reach that durability
            # boundary.  It contains no Session or GTK object, and its late
            # completion is deliberately ineligible to touch the application.
            self._authoring_jobs.shutdown(wait=False, cancel_futures=True)
            self._authoring_jobs = None

    def _hold_authoring_lifetime(self) -> bool:
        """Keep a normal GUI resident until one accepted transaction settles."""
        if self._authoring_shutdown or self._runtime_shutdown:
            return False
        self.hold()
        self._authoring_lifetime_holds += 1
        return True

    def _release_authoring_lifetime(self, task: _AuthoringTask) -> None:
        """Balance exactly the hold acquired for ``task`` at admission."""
        if not task.application_held or self._authoring_lifetime_holds <= 0:
            return
        self._authoring_lifetime_holds -= 1
        self.release()
        self._maybe_finish_requested_quit()

    def _maybe_finish_requested_quit(self) -> None:
        """Honor explicit quit only after every accepted durable tail settles."""
        if not self._quit_requested:
            return
        with self._settings_authoring_lock:
            settings_busy = bool(
                self._settings_authoring_running or self._settings_authoring_pending
            )
        if (
            self._authoring_active
            or self._authoring_queue
            or self._authoring_lifetime_holds
            or self._runtime_publication_held
            or self._runtime_library_scan_held
            or self._removal_convergence_held
            or settings_busy
        ):
            return
        if self._held:
            self.release()
            self._held = False
        self.quit()

    def authoring_off_thread(
        self,
        work: Callable[[], _AuthoringResult],
        finish: Callable[[_AuthoringResult], server.Outcome],
        *,
        prepare: Callable[[], Callable[[], _AuthoringResult]] | None = None,
        finalize: Callable[[], None] | None = None,
        requires_migration: bool = True,
        guard_migration_transaction: bool = True,
        queue_if_busy: bool = False,
        allow_during_quit: bool = False,
    ) -> server.Deferred:
        """Run one durable control mutation without lending GTK to the disk.

        ``work`` must close over only a Store and immutable/value inputs.  Its
        Store transaction rebases under that store's cross-process lock and
        installs memory only after the durable write.  ``finish`` runs later
        on GTK, where Session rotations and visible models can be reconciled.
        It may return another Deferred; Select uses that small composition to
        publish its new Quick choice before the ordered runtime command.
        """

        def start(reply: server.Reply) -> None:
            if (
                self._authoring_shutdown
                or self._runtime_shutdown
                or (self._quit_requested and not allow_during_quit)
            ):
                reply(Response.failure("application is shutting down"))
                return
            if requires_migration and not self._authoring_migration_ready:
                reply(self._migration_blocked_response())
                return
            if not queue_if_busy and (self._authoring_active or self._authoring_queue):
                reply(
                    Response.failure(
                        "another authoring change is still being saved; retry after it finishes",
                        kind="authoring-busy",
                    )
                )
                return
            if queue_if_busy and len(self._authoring_queue) >= MAX_GUI_AUTHORING_QUEUE:
                reply(
                    Response.failure(
                        "too many authoring changes are waiting; let the current saves finish",
                        kind="authoring-busy",
                    )
                )
                return
            application_held = self._hold_authoring_lifetime()
            if not application_held:
                reply(Response.failure("application is shutting down"))
                return
            self._authoring_queue.append(
                _AuthoringTask(
                    work=work,
                    finish=finish,
                    reply=reply,
                    prepare=prepare,
                    finalize=finalize,
                    requires_migration=requires_migration,
                    guard_migration_transaction=guard_migration_transaction,
                    application_held=application_held,
                )
            )
            self._start_next_authoring_task()

        return server.Deferred(start=start)

    def _start_next_authoring_task(self) -> None:
        """Start a transaction only after the previous one has fully replied."""
        if (
            self._authoring_shutdown
            or self._runtime_shutdown
            or self._authoring_active
            or not self._authoring_queue
        ):
            return
        task = self._authoring_queue.popleft()
        self._authoring_active = True
        work = task.work
        if task.requires_migration and not self._authoring_migration_ready:
            task.reply(self._migration_blocked_response())
            self._authoring_task_completed(task)
            return
        if task.prepare is not None:
            try:
                work = task.prepare()
            except Exception as error:
                task.reply(server.failed(error))
                self._authoring_task_completed(task)
                return

        def guarded_work() -> Any:
            # A later explicit CLI import uses this same marker lock. Holding
            # it across the complete worker transaction closes the check/use
            # race between its empty-profile preflight and target install.
            if task.guard_migration_transaction:
                with legacy_migration.unattended_transaction():
                    return work()
            return work()

        try:
            future = self._authoring_pool().submit(guarded_work)
        except RuntimeError:
            if not self._authoring_shutdown and not self._runtime_shutdown:
                task.reply(Response.failure("application is shutting down"))
            self._authoring_task_completed(task)
            return
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_authoring_outcome,
                done,
                task,
            )
        )

    def _finish_authoring_outcome(
        self,
        future: Future[Any],
        task: _AuthoringTask,
    ) -> bool:
        """Adopt one worker result on GTK, optionally chaining a Deferred."""
        if self._authoring_shutdown or self._runtime_shutdown:
            self._authoring_task_completed(task)
            return GLib.SOURCE_REMOVE
        try:
            outcome = task.finish(future.result())
        except Exception as error:
            task.reply(server.failed(error))
            self._authoring_task_completed(task)
            return GLib.SOURCE_REMOVE
        if isinstance(outcome, Response):
            task.reply(outcome)
            self._authoring_task_completed(task)
            return GLib.SOURCE_REMOVE

        def chained_reply(response: Response) -> None:
            if not self._authoring_shutdown and not self._runtime_shutdown:
                task.reply(response)
            self._authoring_task_completed(task)

        try:
            outcome.start(chained_reply)
        except Exception as error:
            task.reply(server.failed(error))
            self._authoring_task_completed(task)
        return GLib.SOURCE_REMOVE

    def _authoring_task_completed(self, task: _AuthoringTask) -> None:
        """Release the actor only after mutation, GTK adoption and reply."""
        self._finalize_authoring_task(task)
        self._authoring_active = False
        self._release_authoring_lifetime(task)
        self._start_next_authoring_task()

    def _finalize_authoring_task(self, task: _AuthoringTask) -> None:
        """Run optional lane cleanup without letting it wedge the actor."""
        if task.finalize is None:
            return
        try:
            task.finalize()
        except Exception as error:  # pragma: no cover - finalizers are tiny state changes
            self.window_report(f"Authoring cleanup failed: {error}")

    def authoring_action_async(
        self,
        work: Callable[[], _AuthoringResult],
        finish: Callable[[_AuthoringResult], None],
        *,
        prepare: Callable[[], Callable[[], _AuthoringResult]] | None = None,
        finalize: Callable[[], None] | None = None,
        failure: Callable[[str], None] | None = None,
        requires_migration: bool = True,
        guard_migration_transaction: bool = True,
        allow_during_quit: bool = False,
    ) -> bool:
        """Queue one GTK action on the same ordered durability actor.

        The visible completion runs on GTK only after the Store transaction is
        durable.  Errors are delivered as plain text so callers can restore
        their exact controls without allowing worker exceptions to escape the
        main loop.
        """
        if self._authoring_shutdown or self._runtime_shutdown:
            return False
        if self._quit_requested and not allow_during_quit:
            return False
        if requires_migration and not self._authoring_migration_ready:
            self.require_authoring_ready()
            return False

        def adopted(result: _AuthoringResult) -> Response:
            finish(result)
            return Response.success("authoring saved")

        synchronous = True
        accepted = True

        def replied(response: Response) -> None:
            nonlocal accepted
            if synchronous and not response.ok:
                accepted = False
            if response.ok:
                return
            if failure is not None:
                failure(response.message)
            else:
                self.window_report(response.message)

        deferred = self.authoring_off_thread(
            work,
            adopted,
            prepare=prepare,
            finalize=finalize,
            requires_migration=requires_migration,
            guard_migration_transaction=guard_migration_transaction,
            queue_if_busy=True,
            allow_during_quit=allow_during_quit,
        )
        deferred.start(replied)
        synchronous = False
        return accepted

    def authoring_barrier(
        self,
        finish: Callable[[], server.Outcome],
    ) -> server.Deferred:
        """Order an authoring-dependent read/runtime action after prior writes."""
        return self.authoring_off_thread(
            lambda: None,
            lambda _nothing: finish(),
            requires_migration=False,
            queue_if_busy=False,
        )

    def legacy_authoring_sync(
        self,
        work: Callable[[], _AuthoringResult],
    ) -> _AuthoringResult:
        """Exclude an explicit compatibility-service write from migration."""
        with legacy_migration.unattended_transaction():
            return work()

    def prepare_pairing_mutation(
        self,
        item: MediaItem,
        mutation: Callable[[pairings.Store, MediaItem], _AuthoringResult],
    ) -> Callable[[], _AuthoringResult]:
        """Revalidate an editor item when its queued gesture reaches the head."""
        current = self.current_item_for_authoring(item)
        store = self._session.pairings
        return lambda: mutation(store, current)

    def prepare_still_pairing_mutation(
        self,
        item: MediaItem,
        requested_still: Path | None,
        mutation: Callable[[pairings.Store, MediaItem, Path | None], pairings.Pairing],
    ) -> Callable[[], _StillPairingMutationResult]:
        """Revalidate both sides of a queued representative-still choice."""
        current = self.current_item_for_authoring(item)
        still = requested_still
        if still is not None:
            still = self._session.library.require_representative_still(still).path
            if still in self._removed_item_paths:
                raise ValueError(f"{still.name} was removed before the still choice could save")
        store = self._session.pairings
        roots = self._session.library.roots

        def work() -> _StillPairingMutationResult:
            effective = still
            if effective is None:
                effective = pairings.synthesize(current, roots).still
            # Resolve the default before the durable mutation. A slow or
            # faulty source path can then fail cleanly without committing a
            # pairing whose GTK adoption/publication callback is skipped.
            record = mutation(store, current, still)
            return _StillPairingMutationResult(current, record, effective)

        return work

    def prepare_pairing_reset(
        self,
        item: MediaItem,
    ) -> Callable[[], _PairingResetMutationResult]:
        """Reset and resolve its default away from GTK before acknowledging."""
        current = self.current_item_for_authoring(item)
        store = self._session.pairings
        roots = self._session.library.roots

        def work() -> _PairingResetMutationResult:
            effective = pairings.synthesize(current, roots).still
            changed = store.reset(current)
            return _PairingResetMutationResult(current, changed, effective)

        return work

    def adopt_pairing_still(
        self,
        item: MediaItem,
        effective_still: Path | None,
    ) -> MediaItem:
        """Replace one accepted item with its worker-resolved still in memory."""
        library = self._session.library
        current = library.find(item.path)
        if current is None or pairings.Identity.of(current) != pairings.Identity.of(item):
            return item
        adopted = current.with_still(effective_still) if current.is_moving else current
        if adopted != current:
            self._session.adopt_library(
                Library(
                    roots=library.roots,
                    items=tuple(
                        adopted if candidate.path == current.path else candidate
                        for candidate in library.items
                    ),
                    skipped=library.skipped,
                    still_inventory=library.still_inventory,
                ),
                reconcile_workshop=False,
            )
        return adopted

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem:
        """Resolve one item after prior queued lifecycle changes have landed."""
        current = self._session.library.find(item.path)
        if (
            item.path in self._removed_item_paths
            or current is None
            or pairings.Identity.of(current) != pairings.Identity.of(item)
        ):
            raise ValueError(f"{item.name} was removed before the authoring change could save")
        return current

    def _send_gui_runtime(
        self,
        verb: str,
        argument: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Response:
        """Send through this application's shutdown-cancellable socket group."""
        return client.send_runtime(
            verb,
            argument,
            timeout=timeout,
            cancellation=self._runtime_cancellation,
        )

    def _send_gui_runtime_on(
        self,
        connector: str,
        verb: str,
        argument: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Response:
        """Send one display command through the cancellable GUI socket group."""
        return client.send_runtime_on(
            connector,
            verb,
            argument,
            timeout=timeout,
            cancellation=self._runtime_cancellation,
        )

    def runtime_off_thread(self, work: Callable[[], Response]) -> server.Deferred:
        """Answer one control request from the ordered runtime worker.

        Display discovery invokes compositor IPC with a bounded but visible
        timeout.  The control socket dispatches on GTK, so it uses the same
        single worker as runtime socket calls and cannot freeze the window or
        overtake a configuration publication.
        """

        def start(reply: Callable[[Response], None]) -> None:
            if self._runtime_shutdown:
                reply(Response.failure("application is shutting down"))
                return
            try:
                future = self._runtime_pool().submit(work)
            except RuntimeError:
                reply(Response.failure("application is shutting down"))
                return
            future.add_done_callback(lambda done: self._deliver_runtime_outcome(done, reply))

        return server.Deferred(start=start)

    def control_runtime_off_thread(
        self,
        verb: str,
        argument: str | None = None,
        *,
        success: Callable[[Response], Response] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> server.Deferred:
        """Run one legacy-fallback control action through the Rust barrier.

        ``ctl`` normally talks straight to Rust.  It reaches the GUI socket
        only after that socket appeared absent, so a daemon which is starting
        or restarting can still answer here.  Compilation, reload and the
        final action stay in the one runtime lane; a 45-second socket deadline
        can therefore never become a 45-second GTK frame.
        """

        def work() -> tuple[Response, bool]:
            return self._execute_gui_runtime_call(lambda: self._send_gui_runtime(verb, argument))

        def start(reply: server.Reply) -> None:
            if self._runtime_shutdown:
                reply(Response.failure("application is shutting down"))
                return
            try:
                future = self._runtime_pool().submit(work)
            except RuntimeError:
                reply(Response.failure("application is shutting down"))
                return
            future.add_done_callback(
                lambda done: self._post_runtime_completion(
                    done,
                    lambda landed: self._finish_control_runtime_outcome(
                        landed,
                        reply,
                        success,
                        on_success,
                    ),
                )
            )

        return server.Deferred(start=start)

    def _finish_control_runtime_outcome(
        self,
        future: Future[tuple[Response, bool]],
        reply: server.Reply,
        success: Callable[[Response], Response] | None,
        on_success: Callable[[], None] | None,
    ) -> bool:
        """Deliver one runtime-backed control answer on GTK's main context."""
        if self._runtime_shutdown:
            return GLib.SOURCE_REMOVE
        try:
            response, runtime_answered = future.result()
        except Exception as error:  # pragma: no cover - defensive executor boundary
            response = Response.failure(f"runtime command failed: {error}")
            runtime_answered = True
        if not runtime_answered:
            self._runtime_status = None
            if self._window is not None:
                self._window.show_runtime_unavailable()
        if response.ok:
            if on_success is not None:
                on_success()
            if success is not None:
                response = success(response)
        reply(response)
        if self._window is not None:
            self.refresh_runtime_status_async()
        return GLib.SOURCE_REMOVE

    def _deliver_runtime_outcome(
        self,
        future: Future[Response],
        reply: Callable[[Response], None],
    ) -> None:
        """Deliver a Deferred control result only while this app still owns it."""
        try:
            response = future.result()
        except Exception as error:  # defensive boundary around discovery too
            response = server.failed(error)

        def deliver() -> bool:
            if not self._runtime_shutdown:
                reply(response)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    @staticmethod
    def _post_runtime_completion(
        future: Future[_RuntimeResult],
        callback: Callable[[Future[_RuntimeResult]], bool],
    ) -> None:
        """Marshal one worker completion back onto GTK's main context."""
        GLib.idle_add(callback, future)

    def _ensure_runtime_compile_queued(self) -> None:
        """Start at most one coalescing compiler job for the latest request."""
        with self._runtime_config_lock:
            if self._runtime_shutdown or self._runtime_compile_pending:
                return
            request = self._runtime_authoring_request
            if request is None or (
                request.generation <= self._runtime_compiled_generation
                and not self._runtime_publication_held
            ):
                return
            self._runtime_compile_pending = True
        try:
            future = self._runtime_pool().submit(self._compile_runtime_until_current)
        except RuntimeError:
            # Executor shutdown can race a final authoring callback.  The
            # request remains uncompiled, but no completion may reach GTK.
            with self._runtime_config_lock:
                self._runtime_compile_pending = False
            return
        future.add_done_callback(
            lambda done: self._post_runtime_completion(done, self._finish_runtime_compile)
        )

    @staticmethod
    def _compile_runtime_request(request: _RuntimeAuthoringRequest) -> bool:
        """Compile one immutable Library plus the latest durable authoring.

        No live application ``Session`` enters this worker.  The short-lived
        session and every one of its stores are constructed and destroyed on
        this thread while the cross-process compiler lock is held.
        """
        with (
            legacy_migration.unattended_transaction(),
            runtime_config.compiler_lock(),
        ):
            settings = config.load_strict()
            durable_sources = (settings.roots, settings.scan_workshop)
            if request.library_sources != durable_sources:
                raise _RuntimeLibraryNotReadyError(
                    "the accepted library belongs to older source settings"
                )
            snapshot = Session(settings)
            try:
                snapshot.adopt_library(request.library, reconcile_workshop=False)
                return runtime_config.update(settings, snapshot)
            finally:
                snapshot.shutdown()

    def _compile_runtime_until_current(
        self,
        *,
        wait_for_library: bool = False,
    ) -> _RuntimePublication:
        """Serialize runtime publication with the physical removal boundary."""
        with self._runtime_removal_lock:
            return self._compile_runtime_until_current_locked(
                wait_for_library=wait_for_library,
            )

    def _compile_runtime_until_current_locked(
        self,
        *,
        wait_for_library: bool = False,
    ) -> _RuntimePublication:
        """Compile/reload through the newest observed authoring generation.

        This method also serves as the barrier at the front of every GUI
        playback action.  Therefore an action already in the executor cannot
        overtake a newer request which arrived while an older compile or
        reload was in flight.
        """
        changed_any = False
        completed = 0
        while True:
            with self._runtime_config_lock:
                request = self._runtime_authoring_request
                compiled = self._runtime_compiled_generation
                shutting_down = self._runtime_shutdown
                scan_pending = self._runtime_library_scan_pending
                scan_error = self._runtime_library_error
                removal_pending = self._removal_runtime_invalidated
                authoring_generation = self._runtime_authoring_generation
            if shutting_down:
                return _RuntimePublication(max(completed, compiled), changed=changed_any)
            if scan_pending and (request is None or wait_for_library):
                # Delete/Trash deliberately removes the old immutable Library
                # before its physical worker starts.  With no replacement
                # request, falling through here would reload the stale runtime
                # document.  Playback gestures also wait for an ordinary
                # source-changing scan; during an active removal they fail
                # promptly so the destructive worker can drain this lane.
                generation = max(completed, compiled, authoring_generation)
                if not wait_for_library or removal_pending:
                    return _RuntimePublication(
                        generation,
                        changed=changed_any,
                        deferred=True,
                    )
                while not self._runtime_library_ready.wait(0.05):
                    with self._runtime_config_lock:
                        if self._runtime_shutdown:
                            return _RuntimePublication(
                                generation,
                                changed=changed_any,
                            )
                        if self._removal_runtime_invalidated:
                            return _RuntimePublication(
                                generation,
                                changed=changed_any,
                                deferred=True,
                            )
                with self._runtime_config_lock:
                    scan_error = self._runtime_library_error
                if scan_error:
                    return _RuntimePublication(
                        generation,
                        changed=changed_any,
                        error=scan_error,
                    )
                continue
            if request is not None and request.generation > compiled:
                try:
                    changed = self._compile_runtime_request(request)
                except _RuntimeLibraryNotReadyError:
                    with self._runtime_config_lock:
                        latest = self._runtime_authoring_request
                        scan_pending = self._runtime_library_scan_pending
                        scan_error = self._runtime_library_error
                    if latest is not None and latest.generation > request.generation:
                        continue
                    if not scan_pending:
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            error=(
                                scan_error
                                or "The library does not match the saved source settings; "
                                "refresh it before controlling playback"
                            ),
                        )
                    if not wait_for_library:
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            deferred=True,
                        )
                    while not self._runtime_library_ready.wait(0.05):
                        with self._runtime_config_lock:
                            if self._runtime_shutdown:
                                return _RuntimePublication(
                                    max(completed, compiled),
                                    changed=changed_any,
                                )
                    with self._runtime_config_lock:
                        scan_error = self._runtime_library_error
                    if scan_error:
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            error=scan_error,
                        )
                    continue
                except (config.ConfigError, runtime_config.RuntimeConfigError) as error:
                    with self._runtime_config_lock:
                        latest = self._runtime_authoring_request
                    if latest is not None and latest.generation > request.generation:
                        # The failed snapshot has already been superseded.  Only
                        # the newest authoring truth is eligible to report an
                        # error or become the last-known-good runtime document.
                        continue
                    return _RuntimePublication(
                        request.generation,
                        changed=changed_any,
                        error=str(error),
                    )
                except Exception as error:  # pragma: no cover - defensive worker boundary
                    return _RuntimePublication(
                        request.generation,
                        changed=changed_any,
                        error=f"runtime configuration worker failed: {error}",
                    )

                with self._runtime_config_lock:
                    current_generation = self._runtime_authoring_generation
                    invalidated = current_generation > request.generation
                    latest = self._runtime_authoring_request
                if invalidated:
                    # A Delete/Trash prepare invalidates the immutable Library
                    # before waiting for this compiler lock. The old compile
                    # may finish, but it is never eligible to reload Rust; a
                    # post-delete scan will replace it with a newer request.
                    if latest is not None and latest.generation > request.generation:
                        continue
                    return _RuntimePublication(
                        current_generation,
                        changed=changed_any or changed,
                        deferred=True,
                    )

                changed_any = changed_any or changed
                completed = request.generation
                with self._runtime_config_lock:
                    self._runtime_compiled_generation = max(
                        self._runtime_compiled_generation,
                        request.generation,
                    )
                    if changed:
                        self._runtime_document_generation += 1
                    latest = self._runtime_authoring_request
                if latest is not None and latest.generation > request.generation:
                    continue
                # Re-sample even when this was current. It closes the tiny
                # interval between the freshness check above and reload.
                continue

            completed = compiled
            with self._runtime_config_lock:
                latest = self._runtime_authoring_request
                scan_pending = self._runtime_library_scan_pending
                scan_error = self._runtime_library_error
                scan_sources = self._runtime_library_scan_sources
                removal_pending = self._removal_runtime_invalidated
                authoring_generation = self._runtime_authoring_generation
            if latest is not None and latest.generation > completed:
                continue
            if (
                scan_error
                and latest is not None
                and scan_sources is not None
                and latest.library_sources != scan_sources
            ):
                # The preceding compile may already have written a document
                # for the last accepted Library before a new-root scan failed.
                # It remains a valid last-known-good candidate, but reloading
                # it now would falsely bless it as the runtime for the newer
                # durable source settings. Keep Rust on its prior LKG and make
                # the scan failure the terminal publication result.
                return _RuntimePublication(
                    max(completed, authoring_generation),
                    changed=changed_any,
                    error=scan_error,
                )
            if scan_pending or authoring_generation > completed:
                # Re-sample instead of reloading an older document.  The next
                # loop either compiles the replacement request, waits for its
                # accepted scan, or returns an explicit in-progress result.
                if not wait_for_library or removal_pending:
                    return _RuntimePublication(
                        max(completed, authoring_generation),
                        changed=changed_any,
                        deferred=True,
                    )
                continue
            try:
                response = self._reload_runtime_if_needed()
            except client.NotRunningError:
                publication = _RuntimePublication(
                    completed,
                    changed=changed_any,
                    unavailable=True,
                )
            except client.ControlError as error:
                publication = _RuntimePublication(
                    completed,
                    changed=changed_any,
                    error=f"Runtime reload failed: {error}",
                )
            else:
                publication = _RuntimePublication(
                    completed,
                    changed=changed_any,
                    response=response,
                )

            # A request can arrive while the socket call is in flight. Absorb
            # it in this outer loop; sustained edits remain constant-stack.
            # A removal can also invalidate the Library *during* the socket
            # call.  Its worker drains this runtime lane before unlinking, and
            # this tail must not bless the just-reloaded stale document.
            with self._runtime_config_lock:
                latest = self._runtime_authoring_request
                scan_pending = self._runtime_library_scan_pending
                removal_pending = self._removal_runtime_invalidated
                authoring_generation = self._runtime_authoring_generation
            if latest is not None and latest.generation > completed:
                continue
            if scan_pending or authoring_generation > completed:
                if not wait_for_library or removal_pending:
                    return _RuntimePublication(
                        max(completed, authoring_generation),
                        changed=changed_any,
                        deferred=True,
                    )
                continue
            return publication

    def _finish_runtime_compile(self, future: Future[_RuntimePublication]) -> bool:
        """Report only a current compiler result, then service a trailing edit."""
        try:
            publication = future.result()
        except Exception as error:  # pragma: no cover - executor boundary
            publication = _RuntimePublication(0, error=f"runtime compiler failed: {error}")
        with self._runtime_config_lock:
            self._runtime_compile_pending = False
            latest = self._runtime_authoring_request
            current = latest is None or latest.generation <= publication.generation
            again = latest is not None and latest.generation > publication.generation
            retry_terminal_scan = (
                publication.deferred
                and not self._runtime_library_scan_pending
                and latest is not None
            )
            publication_hold_terminal = (
                self._runtime_publication_held
                and not publication.deferred
                and publication.generation >= self._runtime_publication_hold_target
            )
            if (
                self._removal_convergence_held
                and self._removal_convergence_runtime_target > 0
                and self._runtime_compiled_generation >= self._removal_convergence_runtime_target
            ):
                self._removal_convergence_runtime_terminal = True
        if not self._runtime_shutdown and current:
            if publication.error:
                self.window_report(publication.error)
            elif publication.unavailable:
                if self._window is not None:
                    self._window.show_runtime_unavailable()
            elif publication.response is not None and not publication.response.ok:
                self.window_report(
                    f"Runtime rejected the new configuration: {publication.response.message}"
                )
            elif publication.changed and self._window is not None:
                self.refresh_runtime_status_async()
        if (again or retry_terminal_scan) and not self._runtime_shutdown:
            self._ensure_runtime_compile_queued()
        if publication_hold_terminal:
            self._release_runtime_publication_hold()
        self._maybe_finish_removal_convergence()
        return GLib.SOURCE_REMOVE

    def _release_runtime_publication_hold(self) -> None:
        """Balance the coalesced hold after its newest terminal publication."""
        if not self._runtime_publication_held:
            return
        self._runtime_publication_held = False
        self._runtime_publication_hold_target = 0
        self.release()
        self._maybe_finish_requested_quit()

    def _queue_runtime_reload(self) -> None:
        """Request one reload, folding a burst of authoring writes into two max.

        A write that lands while the first request is already executing needs
        one trailing reload: the service may have read the document just
        before the newer atomic rename.  Further writes join that trailing
        request instead of growing an unbounded queue.
        """
        if self._runtime_shutdown:
            return
        if self._runtime_reload_pending:
            self._runtime_reload_again = True
            return
        self._runtime_reload_pending = True
        future = self._runtime_pool().submit(self._reload_runtime_if_needed)
        future.add_done_callback(
            lambda done: self._post_runtime_completion(done, self._finish_runtime_reload)
        )

    def _finish_runtime_reload(self, future: Future[Response]) -> bool:
        """Finish a coalesced authoring reload on GTK's thread."""
        self._runtime_reload_pending = False
        if not self._runtime_shutdown:
            try:
                response = future.result()
            except client.NotRunningError:
                if self._window is not None:
                    self._window.show_runtime_unavailable()
            except client.ControlError as error:
                self.window_report(f"Runtime reload failed: {error}")
            else:
                if not response.ok:
                    self.window_report(
                        f"Runtime rejected the new configuration: {response.message}"
                    )
                elif self._window is not None:
                    self.refresh_runtime_status_async()
        again = self._runtime_reload_again
        self._runtime_reload_again = False
        if again and not self._runtime_shutdown:
            self._queue_runtime_reload()
        return GLib.SOURCE_REMOVE

    def _reload_runtime_if_needed(self) -> Response:
        """Bring Rust to the newest atomic document before later commands run.

        A document can change while a previous reload is on the wire.  The
        generation is sampled *before* each request; if it changes before the
        reply, the loop reloads again.  Runtime actions call this in their own
        worker job as a final ordering barrier, so an action already queued
        behind reload A cannot overtake the trailing reload for document B.
        """
        while True:
            with self._runtime_config_lock:
                wanted = self._runtime_document_generation
                loaded = self._runtime_loaded_generation
            if loaded >= wanted:
                return Response.success()
            response = self._send_gui_runtime("reload")
            if not response.ok:
                return response
            with self._runtime_config_lock:
                self._runtime_loaded_generation = max(
                    self._runtime_loaded_generation,
                    wanted,
                )

    def _stop_runtime_status_timer(self) -> None:
        if self._runtime_status_source:
            GLib.source_remove(self._runtime_status_source)
            self._runtime_status_source = 0

    def _start_runtime_status_timer(self) -> None:
        """Keep the GUI on service-owned truth without doing playback work."""
        self._stop_runtime_status_timer()
        self.refresh_runtime_status_async()
        self._runtime_status_source = GLib.timeout_add_seconds(
            RUNTIME_STATUS_TICK_SECONDS, self._on_runtime_status_tick
        )

    def _on_runtime_status_tick(self) -> bool:
        if self._window is None:
            self._runtime_status_source = 0
            return GLib.SOURCE_REMOVE
        self.refresh_runtime_status_async()
        return GLib.SOURCE_CONTINUE

    def refresh_runtime_status_async(self) -> bool:
        """Queue one status probe without ever waiting in a GTK callback.

        Timer ticks coalesce while a probe is in flight.  A request from a
        newly created window instead asks for one trailing probe, because the
        old generation's result is deliberately ineligible to update it.
        A missing socket updates the unavailable UI but never starts the
        compatibility renderer. That renderer belongs only to explicit
        headless ``--service`` mode.
        """
        window = self._window
        if window is None or self._runtime_shutdown:
            return False
        generation = self._window_generation
        if self._runtime_status_pending:
            if self._runtime_status_generation != generation:
                self._runtime_status_again = True
            return False
        self._runtime_status_pending = True
        self._runtime_status_generation = generation
        future = self._runtime_pool().submit(self._request_runtime_status)

        def completed(done: Future[_RuntimeStatusReply]) -> None:
            def deliver(landed: Future[_RuntimeStatusReply]) -> bool:
                return self._finish_runtime_status(landed, window, generation)

            self._post_runtime_completion(done, deliver)

        future.add_done_callback(completed)
        return True

    def _request_runtime_status(self) -> _RuntimeStatusReply:
        """Fetch and parse a possibly large status document off GTK."""
        response = self._send_gui_runtime("status", timeout=0.25)
        if not response.ok:
            return _RuntimeStatusReply(response)
        try:
            decoded: object = json.loads(response.message)
        except ValueError, RecursionError:
            return _RuntimeStatusReply(
                response,
                protocol_error="Runtime returned a status reply that was not valid JSON",
            )
        if not (
            isinstance(decoded, dict)
            and isinstance(decoded.get("playlist"), str)
            and isinstance(decoded.get("source"), str)
        ):
            return _RuntimeStatusReply(
                response,
                protocol_error="Runtime returned a status reply without playlist state",
            )
        return _RuntimeStatusReply(
            response,
            decoded,
            status_document=response.message,
        )

    def _finish_runtime_status(
        self,
        future: Future[_RuntimeStatusReply],
        window: MainWindow,
        generation: int,
    ) -> bool:
        """Render a status result only into the window that requested it."""
        self._runtime_status_pending = False
        if self._runtime_shutdown:
            self._runtime_status_again = False
            return GLib.SOURCE_REMOVE
        current = self._window is window and self._window_generation == generation
        try:
            reply = future.result()
        except client.NotRunningError:
            if current:
                self._runtime_status = None
                window.show_runtime_unavailable()
        except client.ControlError:
            # A missed deadline is not proof that nobody owns the runtime.
            # Retain the last atomic answer rather than making the header and
            # authoring pages jump to Python's stale Session state. Crucially,
            # no compatibility callback is allowed to start a second driver.
            if current:
                window.show_runtime_delayed()
        else:
            if current:
                if not reply.response.ok:
                    window.show_runtime_protocol_error(
                        f"Runtime rejected its status request: {reply.response.message}"
                    )
                elif reply.protocol_error:
                    window.show_runtime_protocol_error(reply.protocol_error)
                else:
                    assert reply.status is not None
                    self._adopt_runtime_status(
                        reply.status,
                        window,
                        health_document=reply.status_document,
                    )
        again = self._runtime_status_again
        self._runtime_status_again = False
        if again and self._window is not None and not self._runtime_shutdown:
            self.refresh_runtime_status_async()
        return GLib.SOURCE_REMOVE

    def _runtime_is_running(self) -> bool:
        try:
            response = client.send_runtime("status", timeout=0.25)
        except client.NotRunningError:
            self._runtime_status = None
            if self._window is not None:
                self._window.show_runtime_unavailable()
            return False
        except client.ControlError:
            # A timeout or malformed answer is not proof that the process is
            # absent.  In particular, the retained Python fallback must not
            # start applying wallpapers merely because a busy Rust runtime
            # missed one status deadline.
            if self._window is not None:
                self._window.show_runtime_delayed()
            return True
        if self._window is not None and not response.ok:
            self._window.show_runtime_protocol_error(
                f"Runtime rejected its status request: {response.message}"
            )
        elif response.ok:
            try:
                status: object = json.loads(response.message)
            except ValueError, RecursionError:
                if self._window is not None:
                    self._window.show_runtime_protocol_error(
                        "Runtime returned a status reply that was not valid JSON"
                    )
            else:
                if (
                    isinstance(status, dict)
                    and isinstance(status.get("playlist"), str)
                    and isinstance(status.get("source"), str)
                ):
                    self._adopt_runtime_status(
                        status,
                        self._window,
                        health_document=response.message,
                    )
                else:
                    if self._window is not None:
                        self._window.show_runtime_protocol_error(
                            "Runtime returned a status reply without playlist state"
                        )
        # Even a rejected status request proves that this socket has an owner;
        # do not turn a protocol failure into permission for a second driver.
        return True

    def _adopt_runtime_status(
        self,
        status: dict[str, object],
        window: MainWindow | None,
        *,
        health_document: str | None = None,
    ) -> None:
        """Show one atomic snapshot and queue guarded health persistence.

        Missing reports never clear health.  Mapping entries, taking the
        compiler lock, reloading stores, rendering a large document and
        fsyncing Pairings all belong to the ordered worker, never GTK.
        """
        self._runtime_status = status
        omitted = status.get("taboo_entries_omitted", 0)
        if type(omitted) is int and omitted > self._taboo_omitted_seen:
            self._taboo_omitted_seen = omitted
            self.window_report(
                f"The runtime has {omitted} older playback warnings not included in this "
                "status update; existing saved warnings were retained"
            )
        if window is not None:
            window.show_runtime_status(status)
        reports = status.get("taboo_entries")
        if not isinstance(reports, list) or not reports:
            return
        if not self._authoring_migration_ready:
            # Runtime truth remains readable while migration is undecided,
            # but health ingestion is a Pairings mutation and must stay inert.
            return
        if health_document is None:
            try:
                health_document = json.dumps(status)
            except TypeError, ValueError:
                return
        with self._runtime_config_lock:
            authoring_generation = self._runtime_authoring_generation
        request = _RuntimeHealthRequest(
            authoring_generation,
            self._session.library,
            health_document,
        )
        if window is None:
            self._apply_runtime_health_result(self._ingest_runtime_health(request))
            return
        self._queue_runtime_health(request)

    def _queue_runtime_health(self, request: _RuntimeHealthRequest) -> None:
        """Coalesce health persistence through the total authoring order."""
        if not self._authoring_migration_ready:
            return
        with self._runtime_config_lock:
            self._runtime_health_request = request
            if self._runtime_shutdown or self._runtime_health_pending:
                return
            self._runtime_health_pending = True

        def prepare() -> Callable[[], _RuntimeHealthResult]:
            with self._runtime_config_lock:
                generation = self._runtime_authoring_generation
            if request.library is not self._session.library or (
                request.authoring_generation != generation
            ):
                return lambda: _RuntimeHealthResult(request)
            return lambda: self._ingest_runtime_health(request)

        def finished(result: _RuntimeHealthResult) -> None:
            self._complete_runtime_health(result)

        def failed(message: str) -> None:
            self._complete_runtime_health(
                _RuntimeHealthResult(
                    request,
                    error=("" if "authoring" in message.casefold() else message),
                )
            )

        accepted = self.authoring_action_async(
            lambda: _RuntimeHealthResult(request),
            finished,
            prepare=prepare,
            failure=failed,
            # The health transaction itself holds the migration marker only
            # around Pairings/config compilation, then releases it before a
            # potentially slow runtime reload.
            guard_migration_transaction=False,
        )
        if not accepted:
            with self._runtime_config_lock:
                self._runtime_health_pending = False

    def _ingest_runtime_health(self, request: _RuntimeHealthRequest) -> _RuntimeHealthResult:
        """Validate, persist, compile and reload one health snapshot off GTK."""
        try:
            status = runtime_health.parse_status(request.status_document)
        except runtime_health.RuntimeHealthError:
            return _RuntimeHealthResult(request)

        snapshot: Session | None = None
        try:
            # Health is opportunistic. If a headless compiler owns the gate,
            # retain the visible status and retry on the next two-second poll.
            with (
                legacy_migration.unattended_transaction(timeout=0),
                runtime_config.compiler_lock(timeout=0),
            ):
                settings = config.load_strict()
                snapshot = Session(settings)
                snapshot.adopt_library(request.library, reconcile_workshop=False)
                faults = snapshot.authoring_faults()
                if faults:
                    return _RuntimeHealthResult(request)

                expected_path = paths.runtime_config_path().absolute()
                status_path = status.get("config_path")
                status_generation = status.get("config_generation")
                if (
                    not isinstance(status_path, str)
                    or not isinstance(status_generation, str)
                    or Path(status_path) != expected_path
                    or runtime_config.read_config_generation(expected_path) != status_generation
                    or runtime_config.document_generation(runtime_config.render(settings, snapshot))
                    != status_generation
                ):
                    return _RuntimeHealthResult(request)

                inventory = runtime_health.taboo_inventory(
                    status,
                    snapshot.playlists.all(),
                    snapshot.library.items,
                )
                if not inventory.reports:
                    return _RuntimeHealthResult(request)
                baseline = dict(snapshot.pairings.records)
                changed, accepted = snapshot.pairings.mark_borked_many_if_unchanged(
                    ((report.item, report.reason, report.source) for report in inventory.reports),
                    baseline,
                )
                if not accepted:
                    return _RuntimeHealthResult(request)

                document_changed = runtime_config.update(settings, snapshot)
                # An unchanged compiled document already contains these
                # markers. Re-sending the same stale/non-durable test reply on
                # every poll would create a reload loop.
                force_reload = document_changed or (
                    changed > 0 and any(not report.durable for report in inventory.reports)
                )
            # Socket I/O is ordered on this worker but is not part of the
            # filesystem transaction. Release the compiler gate first so a
            # slow or absent daemon cannot stall a headless preflight.
            if force_reload:
                with self._runtime_config_lock:
                    self._runtime_document_generation += 1
                try:
                    response = self._reload_runtime_if_needed()
                except client.NotRunningError:
                    publication = _RuntimePublication(
                        request.authoring_generation,
                        changed=document_changed,
                        unavailable=True,
                    )
                except client.ControlError as error:
                    publication = _RuntimePublication(
                        request.authoring_generation,
                        changed=document_changed,
                        error=f"Runtime reload failed: {error}",
                    )
                else:
                    publication = _RuntimePublication(
                        request.authoring_generation,
                        changed=document_changed,
                        response=response,
                    )
            else:
                publication = None
            return _RuntimeHealthResult(
                request,
                changed=changed,
                accepted=True,
                store=snapshot.pairings,
                publication=publication,
            )
        except (
            config.ConfigError,
            legacy_migration.MigrationError,
            runtime_config.RuntimeConfigError,
        ):
            # Busy, stale or unreadable authoring is not a new UI error.  It is
            # safer to consume no fault and retry a later coherent generation.
            return _RuntimeHealthResult(request)
        except pairings.PairingError as error:
            return _RuntimeHealthResult(
                request,
                error=f"Could not save wallpaper playback availability: {error}",
            )
        finally:
            if snapshot is not None:
                snapshot.shutdown()

    def _complete_runtime_health(self, result: _RuntimeHealthResult) -> None:
        """Adopt one actor-ordered health result and launch any coalesced tail."""
        with self._runtime_config_lock:
            self._runtime_health_pending = False
            latest = self._runtime_health_request
            authoring_generation = self._runtime_authoring_generation
        if not self._runtime_shutdown:
            self._apply_runtime_health_result(result, authoring_generation)
        if latest is not None and latest is not result.request and not self._runtime_shutdown:
            self._queue_runtime_health(latest)

    def _apply_runtime_health_result(
        self,
        result: _RuntimeHealthResult,
        authoring_generation: int | None = None,
    ) -> None:
        """Apply a completed health transaction without any filesystem work."""
        if result.error:
            self.window_report(result.error)
            return
        current_generation = (
            self._runtime_authoring_generation
            if authoring_generation is None
            else authoring_generation
        )
        if (
            result.accepted
            and result.store is not None
            and current_generation == result.request.authoring_generation
            and self._session.library is result.request.library
        ):
            self._session.adopt_pairing_store(result.store)
            if result.changed and self._window is not None:
                self._window.pairing_health_changed(self._session)
        publication = result.publication
        if publication is None:
            return
        if publication.error:
            self.window_report(publication.error)
        elif publication.unavailable:
            if self._window is not None:
                self._window.show_runtime_unavailable()
        elif publication.response is not None and not publication.response.ok:
            self.window_report(
                f"Runtime rejected the new configuration: {publication.response.message}"
            )

    def refresh_runtime_status(self) -> bool:
        """Synchronously refresh state for non-GUI compatibility callers."""
        return self._runtime_is_running()

    def _gui_runtime_call_pending(self) -> bool:
        """Reject a duplicate gesture before it mutates local authoring state."""
        if not self._runtime_action_pending and not self._quick_choice_pending:
            return False
        self.window_report("A playback command is already in progress")
        return True

    def _start_gui_runtime_call(
        self,
        work: Callable[[], Response],
        *,
        on_success: Callable[[bool], None] | None = None,
        on_complete: Callable[[Response, bool], None] | None = None,
        owns_quick_choice: bool = False,
    ) -> bool:
        """Run one GUI command on the ordered socket worker.

        An ordinary GUI is an authoring client of the Rust daemon. A missing
        socket is therefore an actionable failure, never permission to run the
        legacy Python renderer on GTK's main thread or create a second
        wallpaper owner. Only the explicit headless ``--service`` mode retains
        that compatibility renderer through the synchronous command methods.
        """
        if self._runtime_action_pending or (self._quick_choice_pending and not owns_quick_choice):
            self.window_report("A playback command is already in progress")
            return False
        if self._runtime_shutdown:
            return False
        window = self._window
        generation = self._window_generation
        self._runtime_action_pending = True
        if window is not None:
            window.set_runtime_busy(True)
        try:
            future = self._runtime_pool().submit(self._execute_gui_runtime_call, work)
        except RuntimeError:
            self._runtime_action_pending = False
            if window is not None:
                window.set_runtime_busy(False)
            if on_complete is not None:
                on_complete(Response.failure("application is shutting down"), False)
            return False

        def completed(done: Future[tuple[Response, bool]]) -> None:
            def deliver(landed: Future[tuple[Response, bool]]) -> bool:
                return self._finish_gui_runtime_call(
                    landed,
                    window,
                    generation,
                    on_success,
                    on_complete,
                )

            self._post_runtime_completion(done, deliver)

        future.add_done_callback(completed)
        return True

    def _execute_gui_runtime_call(
        self,
        work: Callable[[], Response],
    ) -> tuple[Response, bool]:
        """Do ordered runtime socket I/O off GTK's main thread."""
        try:
            publication = self._compile_runtime_until_current(wait_for_library=True)
            if publication.error:
                return Response.failure(publication.error), True
            if publication.deferred:
                return Response.failure("the library refresh is still in progress"), True
            if publication.unavailable:
                return Response.failure("the wallpaper runtime is unavailable"), False
            if publication.response is not None and not publication.response.ok:
                return publication.response, True
            reloaded = self._reload_runtime_if_needed()
            if not reloaded.ok:
                return reloaded, True
            return work(), True
        except client.NotRunningError:
            return Response.failure("the wallpaper runtime is unavailable"), False
        except client.ControlError as error:
            return Response.failure(str(error)), True
        except Exception as error:  # pragma: no cover - defensive worker boundary
            return Response.failure(f"runtime command failed: {error}"), True

    def _finish_gui_runtime_call(
        self,
        future: Future[tuple[Response, bool]],
        window: MainWindow | None,
        generation: int,
        on_success: Callable[[bool], None] | None,
        on_complete: Callable[[Response, bool], None] | None,
    ) -> bool:
        """Complete a GUI runtime command on GTK's thread."""
        try:
            response, runtime_answered = future.result()
        except Exception as error:  # pragma: no cover - defensive worker boundary
            response = Response.failure(f"runtime command failed: {error}")
            runtime_answered = True
        if not runtime_answered:
            # A definitive missing socket retires the last Rust snapshot.  The
            # authoring GUI does not take rendering ownership, so the honest
            # next state is unavailable rather than stale playback truth.
            self._runtime_status = None
            if self._window is not None:
                self._window.show_runtime_unavailable()
        if response.ok and on_success is not None:
            on_success(runtime_answered)
        self._runtime_action_pending = False
        if on_complete is not None:
            on_complete(response, runtime_answered)
        current = (
            not self._runtime_shutdown
            and window is not None
            and self._window is window
            and self._window_generation == generation
        )
        if current and window is not None:
            window.set_runtime_busy(False)
            if not response.ok:
                window.report(response.message)
            self.refresh_runtime_status_async()
        elif not self._runtime_shutdown and self._window is not None:
            # A command belongs to the application, not to one incarnation of
            # its window.  Its result stays away from a reopened window, but
            # the global single-flight busy flag still has to be released.
            self._window.set_runtime_busy(False)
            self.refresh_runtime_status_async()
        return GLib.SOURCE_REMOVE

    def runtime_action_async(self, verb: str, argument: str | None = None) -> bool:
        """Drive a runtime control from GTK without blocking its main loop."""
        return self._start_gui_runtime_call(lambda: self._send_gui_runtime(verb, argument))

    def runtime_action_on_async(
        self,
        connector: str,
        verb: str,
        argument: str | None = None,
    ) -> bool:
        """Drive exactly one live display, never through the global fallback."""
        return self._start_gui_runtime_call(
            lambda: self._send_gui_runtime_on(connector, verb, argument),
        )

    def reset_display_modes_on_async(self, connector: str) -> bool:
        """Drop both per-display mode overrides in one ordered GUI action."""

        def work() -> Response:
            cycle = self._send_gui_runtime_on(connector, "cycle", "default")
            if not cycle.ok:
                return Response.failure(
                    f"Cycle default was refused; Shuffle was not changed: {cycle.message}"
                )
            shuffle = self._send_gui_runtime_on(connector, "shuffle", "default")
            if not shuffle.ok:
                return Response.failure(
                    "Cycle returned to its saved default, but Shuffle was refused: "
                    f"{shuffle.message}"
                )
            return Response.success("Cycle and Shuffle now use their saved defaults")

        return self._start_gui_runtime_call(work)

    def runtime_action(self, verb: str, argument: str | None = None) -> Response:
        """Drive Rust, with Python rendering only in explicit legacy service mode."""
        try:
            response = client.send_runtime(verb, argument)
        except client.NotRunningError:
            self._runtime_status = None
            if self._window is not None:
                self._window.show_runtime_unavailable()
            if not self._service_start:
                return Response.failure("the wallpaper runtime is unavailable")
            actions: dict[str, Callable[[], Applied]] = {
                "next": self._session.next,
                "previous": self._session.previous,
                "random": self._session.random,
            }
            action = actions.get(verb)
            if action is None:
                return Response.failure(
                    f"the wallpaper runtime is unavailable; cannot {verb} playback"
                )
            return self.apply(action)
        except client.ControlError as error:
            return Response.failure(str(error))
        return response

    def play_item(self, item: MediaItem) -> Response:
        """Compile Media's Quick choice, then let the runtime apply it."""
        health = self._session.pairings.health(pairings.Identity.of(item))
        if health.is_borked:
            return Response.failure(
                f"Playback unavailable for {item.name}; see Library for details and removal options"
            )
        try:
            chosen = (
                self.legacy_authoring_sync(lambda: self._session.choose(item.path))
                if self._service_start
                else self._session.choose(item.path)
            )
        except ApplyError as error:
            return Response.failure(str(error))
        if not self._publish_runtime():
            return Response.failure("the Quick choice could not be published to the runtime")
        try:
            response = client.send_runtime("playlist-use", chosen.id)
        except client.NotRunningError:
            # Only the explicitly requested ``--service`` compatibility mode
            # may own a Python renderer.  A normal authoring GUI must fail
            # honestly instead of blocking GTK and becoming a second owner.
            response = (
                self.apply(self._session.apply_current)
                if self._service_start
                else Response.failure("the wallpaper runtime is unavailable")
            )
        except client.ControlError as error:
            response = Response.failure(str(error))
        if self._window is not None:
            self._window.playlists_changed(self._session)
        return response

    def play_item_async(self, item: MediaItem) -> bool:
        """Compile and apply Media's Quick choice without blocking GTK."""
        if self._gui_runtime_call_pending():
            return False
        health = self._session.pairings.health(pairings.Identity.of(item))
        if health.is_borked:
            self.window_report(
                f"Playback unavailable for {item.name}; see Library for details and removal options"
            )
            return False
        self._quick_choice_pending = True

        def release_choice(_response: Response | None = None, _answered: bool = False) -> None:
            self._quick_choice_pending = False

        def prepare() -> Callable[[], playlists.Playlist]:
            current = self._session.library.find(item.path)
            if current is None or current.path in self._removed_item_paths:
                raise ValueError(f"{item.name} was removed before Quick choice could save")
            health = self._session.pairings.health(pairings.Identity.of(current))
            if health.is_borked:
                raise ValueError(f"Playback unavailable for {current.name}")
            playlist_store = self._session.playlists
            return lambda: playlist_store.set_singleton(
                QUICK_CHOICE_ID,
                QUICK_CHOICE_NAME,
                current.path,
            )

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        def saved(chosen: playlists.Playlist) -> None:
            try:
                self._session.use_playlist(chosen.id)
            except ApplyError as error:
                release_choice()
                self.window_report(str(error))
                return
            self.playlists_changed()
            started = self._start_gui_runtime_call(
                lambda: self._send_gui_runtime("playlist-use", chosen.id),
                on_success=shown,
                on_complete=release_choice,
                owns_quick_choice=True,
            )
            if not started:
                release_choice()

        def unprepared() -> playlists.Playlist:  # pragma: no cover
            raise RuntimeError("Quick choice was not prepared")

        def failed(error: str) -> None:
            release_choice()
            self.window_report(f"The Quick choice could not be saved; nothing changed: {error}")

        accepted = self.authoring_action_async(
            unprepared,
            saved,
            prepare=prepare,
            failure=failed,
        )
        if not accepted:
            release_choice()
        return accepted

    def play_item_on_async(self, item: MediaItem, connector: str) -> bool:
        """Publish one display's singleton Quick choice, then target only it.

        The connector-derived id is stable across app restarts and choices, so
        this replaces one inspectable playlist per display instead of filling
        the authoring store with disposable rows.  A Python fallback would
        have only one cursor and would therefore be a second, global driver;
        connector commands fail honestly when Rust is unavailable.
        """
        if self._gui_runtime_call_pending():
            return False
        if self._session.library.find(item.path) is None:
            self.window_report(f"Not in the library: {item.path}")
            return False
        health = self._session.pairings.health(pairings.Identity.of(item))
        if health.is_borked:
            self.window_report(
                f"Playback unavailable for {item.name} on {connector}; remove or uninstall it first"
            )
            return False
        self._quick_choice_pending = True

        def release_choice(_response: Response | None = None, _answered: bool = False) -> None:
            self._quick_choice_pending = False

        def prepare() -> Callable[[], playlists.Playlist]:
            current = self._session.library.find(item.path)
            if current is None or current.path in self._removed_item_paths:
                raise ValueError(f"{item.name} was removed before Quick choice could save")
            health = self._session.pairings.health(pairings.Identity.of(current))
            if health.is_borked:
                raise ValueError(f"Playback unavailable for {current.name}")
            playlist_store = self._session.playlists
            return lambda: playlist_store.set_display_singleton(
                connector,
                current.path,
                entry_id=runtime_config.entry_id_for_source(current.path),
            )

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        def saved(chosen: playlists.Playlist) -> None:
            self.playlists_changed()
            started = self._start_gui_runtime_call(
                lambda: self._send_gui_runtime_on(connector, "playlist-use", chosen.id),
                on_success=shown,
                on_complete=release_choice,
                owns_quick_choice=True,
            )
            if not started:
                release_choice()

        def unprepared() -> playlists.Playlist:  # pragma: no cover
            raise RuntimeError("display Quick choice was not prepared")

        def failed(error: str) -> None:
            release_choice()
            self.window_report(f"The Quick choice for {connector} could not be saved: {error}")

        accepted = self.authoring_action_async(
            unprepared,
            saved,
            prepare=prepare,
            failure=failed,
        )
        if not accepted:
            release_choice()
        return accepted

    def _make_missing_stills(self) -> None:
        """Fill in the stills for videos that have none, in the background.

        Without this a video only gets its still at the moment dynamics are
        switched off, and only the one video that was playing. Every other one
        keeps dropping out of the rotation when dynamics are off, and keeps
        leaving Noctalia's palette derived from whatever was on screen before.
        """
        # The root the accepted scan actually read from. A still has to go
        # somewhere `pairing` will look, which means inside that library
        # snapshot rather than merely beside a configured-but-missing path.
        # Never turn a discovered-but-unconfirmed Noctalia directory into a
        # write target.  Only Settings.roots records the first-run choice.
        if not self._settings.roots:
            return
        roots = self._session.library.roots
        if not roots:
            return
        self._stills.request(self._session.library.items, roots[0], self._on_stills_made)

    def regenerate_scene_still(self, item: MediaItem) -> bool:
        """Queue an explicit scene recapture; return whether it could start."""
        if not self.require_authoring_ready():
            return False
        if not self._settings.roots:
            self.window_report("Choose a library folder in Settings before generating stills")
            return False
        roots = self._session.library.roots
        if not roots or not item.scene:
            return False
        self._stills.regenerate_scene(item, roots[0], self._on_stills_made)
        return True

    def _on_stills_made(self, made: int) -> None:
        """A batch finished, so the pairings it wrote are worth re-reading.

        Safe against looping: `StillMaker` remembers every video it has
        attempted, so this rescan cannot queue the same work again.
        """
        del made
        self.refresh_library()

    def favourites_changed(self) -> None:
        """Bring everything that reads the favourites back into line.

        Two readers, and neither may be left behind by a star toggled over the
        socket. The rotation is narrowed from the same store when
        `cycle_favourites_only` is on, which is `Session.favourites_changed`'s
        job; the window's counts and its favourites view come from a rebuild.
        This is the pair the window's own star button already does.
        """
        self._session.favourites_changed()
        self._publish_runtime_for_context()
        if self._window is not None:
            self._window.show_library(self._session)

    def remove_item_async(
        self,
        item: MediaItem,
        *,
        trash: bool,
        finish: Callable[[RemovalResult], None],
    ) -> bool:
        """Run one entire destructive lifecycle on the detached actor worker."""

        def prepare() -> Callable[[], RemovalResult]:
            if self._removal_active or self._removal_convergence_held:
                raise removals.RemovalJournalError(
                    "busy", "another wallpaper removal or cleanup refresh is still in progress"
                )
            current = self._session.library.find(item.path)
            if current is None:
                raise ValueError(f"{item.path} is no longer in the library")
            if (current.kind, current.scene, current.provider) != (
                item.kind,
                item.scene,
                item.provider,
            ):
                raise ValueError(f"{item.path} changed before removal could start")
            plan = self._session.prepare_removal_plan(current, trash=trash)
            self._invalidate_runtime_for_pending_removal()
            self._removal_active = True

            def run() -> RemovalResult:
                with (
                    self._runtime_removal_lock,
                    legacy_migration.unattended_transaction(),
                    runtime_config.compiler_lock(),
                ):
                    return plan.run()

            return run

        def adopted(result: RemovalResult) -> None:
            self._adopt_item_removal(result)
            finish(result)

        def unprepared() -> RemovalResult:  # pragma: no cover - prepare always replaces it
            raise RuntimeError("removal transaction was not prepared")

        def failed(message: str) -> None:
            self.window_report(message)

        return self.authoring_action_async(
            unprepared,
            adopted,
            prepare=prepare,
            finalize=self._release_removal_lane,
            failure=failed,
            guard_migration_transaction=False,
        )

    def _release_removal_lane(self) -> None:
        """Clear the destructive-operation guard on every actor exit path."""
        self._removal_active = False
        self._restore_runtime_after_uncommitted_removal()
        if self._refresh_after_removal:
            self._refresh_after_removal = False
            GLib.idle_add(self.refresh_library)

    def _begin_removal_convergence(self) -> None:
        """Keep the process alive through one committed post-delete refresh."""
        if self._removal_convergence_held:
            return
        self.hold()
        self._removal_convergence_held = True
        self._removal_convergence_scan_generation = None
        self._removal_convergence_scan_terminal = False
        self._removal_convergence_runtime_target = 0
        self._removal_convergence_runtime_terminal = False

    def _maybe_finish_removal_convergence(self) -> None:
        """Release the committed tail only after scan and runtime durability."""
        if not self._removal_convergence_held:
            return
        if not (
            self._removal_convergence_scan_terminal and self._removal_convergence_runtime_terminal
        ):
            return
        self._removal_convergence_held = False
        self._removal_convergence_scan_generation = None
        self._removal_convergence_scan_terminal = False
        self._removal_convergence_runtime_target = 0
        self._removal_convergence_runtime_terminal = False
        self.release()
        self._maybe_finish_requested_quit()

    def _adopt_filtered_library_after_removal(self, result: RemovalResult) -> None:
        """Make deleted identities disappear before any later actor prepare."""
        removed = {result.item.path}
        for cleanup in result.cleanups:
            removed.update(cleanup.removed_stills)
        current = self._session.library
        filtered = Library(
            roots=current.roots,
            items=tuple(item for item in current.items if item.path not in removed),
            skipped=current.skipped,
            still_inventory=tuple(
                item for item in current.still_inventory if item.path not in removed
            ),
        )
        self._removed_item_paths.update(removed)
        self._session.adopt_library(filtered, reconcile_workshop=False)
        if self._window is not None:
            self._window.show_library(self._session)

    def _adopt_item_removal(self, result: RemovalResult) -> None:
        """Install only semantic worker deltas after its lease is released."""
        self._removal_active = False
        self._session.adopt_library_cleanups(result.cleanups)
        self._session.adopt_library_fault_repairs(result.repaired_faults)
        refresh = result.committed or self._refresh_after_removal
        self._refresh_after_removal = False
        if result.committed:
            self._invalidate_library_scan_after_removal()
            self._finish_runtime_removal_invalidation()
            self._adopt_filtered_library_after_removal(result)
            self._stills.forget(result.item.path)
            self._begin_removal_convergence()
            # Publish the immutable filtered Library immediately.  The fresh
            # scan below remains authoritative for reinstall detection, but a
            # failed scan can no longer leave Rust naming a deleted source.
            self._publish_runtime_for_context()
            # A new detached refresh starts only after the removal worker has
            # released its operation lease and the actor has adopted every
            # semantic cleanup delta.
        if refresh:
            GLib.idle_add(self.refresh_library)

    def _invalidate_runtime_for_pending_removal(self) -> None:
        """Cancel stale health/compile requests before physical deletion."""
        with self._runtime_config_lock:
            self._runtime_authoring_generation += 1
            self._runtime_authoring_request = None
            self._runtime_health_request = None
            self._runtime_library_scan_pending = True
            self._runtime_library_error = ""
            self._runtime_library_ready.clear()
            self._removal_runtime_invalidated = True

    def _invalidate_library_scan_after_removal(self) -> None:
        """Forbid a pre-delete scan from masquerading as a reinstall."""
        self._library_scan_generation += 1
        if self._window is None:
            return
        request = self._library_scan_request
        self._library_scan_request = None
        if request is not None:
            request.cancel()
        future = self._library_scan_future
        self._library_scan_future = None
        if future is not None:
            future.cancel()

    def _finish_runtime_removal_invalidation(self) -> None:
        """Keep the compiler barred until the post-delete scan is accepted."""
        with self._runtime_config_lock:
            self._runtime_library_scan_generation = self._library_scan_generation
            self._runtime_library_scan_sources = (
                self._settings.roots,
                self._settings.scan_workshop,
            )
            self._runtime_library_scan_pending = True
            self._runtime_library_error = ""
            self._runtime_library_ready.clear()
            self._removal_runtime_invalidated = False

    def _restore_runtime_after_uncommitted_removal(self) -> None:
        """Release a speculative barrier when no physical lifecycle committed."""
        with self._runtime_config_lock:
            if not self._removal_runtime_invalidated:
                return
            self._removal_runtime_invalidated = False
            self._runtime_library_scan_pending = False
            self._runtime_library_error = ""
            self._runtime_library_ready.set()
        self._publish_runtime_for_context()

    def prepare_item_removal(self, item: MediaItem) -> removals.Intent:
        """Legacy synchronous removal boundary for explicit compatibility mode."""
        return self._session.prepare_removal(item, self._session.library.roots)

    def cancel_item_removal(self, intent: removals.Intent) -> tuple[str, ...]:
        """Discard a prepared intent after a physical operation was refused."""
        try:
            self._session.cancel_removal(intent)
        except removals.RemovalJournalError as error:
            return (f"removal journal: {error}",)
        return ()

    def forget_item(
        self,
        item: MediaItem,
        *,
        intent: removals.Intent,
        artifacts_already_clean: bool = True,
        artifact_source_root: Path | None = None,
        artifact_lookup_root: Path | None = None,
        artifact_lookup_parent: Path | None = None,
        artifact_source_context: file_io.PinnedDirectoryContext | None = None,
    ) -> tuple[str, ...]:
        """Finish one committed deletion, retaining its journal until clean."""
        if (item.path, item.kind, item.scene) != (intent.path, intent.kind, intent.scene):
            raise ValueError("removal intent does not identify the removed library item")
        failures = self._session.commit_removal(
            intent,
            artifacts_already_clean=artifacts_already_clean,
            artifact_source_root=artifact_source_root,
            artifact_lookup_root=artifact_lookup_root,
            artifact_lookup_parent=artifact_lookup_parent,
            artifact_source_context=artifact_source_context,
        )
        self._stills.forget(item.path)
        GLib.idle_add(self.refresh_library)
        return failures

    def playlists_changed(self) -> None:
        """Publish one playlist edit without turning it into a library rescan."""
        self._session.playlists_changed()
        self._publish_runtime_for_context()
        if self._window is not None:
            self._window.playlists_changed(self._session)

    def _prepare_playlist_delete(
        self,
        reference: str,
    ) -> Callable[[], _PlaylistDeleteResult]:
        """Resolve a delete only when it reaches the actor head on GTK."""
        playlist = self._session.playlists.find(reference)
        playlist_store = self._session.playlists
        schedule_store = self._session.schedules
        display_store = self._session.displays

        def work() -> _PlaylistDeleteResult:
            # The playlist is the authoritative lifecycle boundary.  Once it
            # commits, every independent cleanup is attempted and reported;
            # throwing on the first tail would hide a real deletion and leave
            # later references needlessly dangling.
            playlist_store.delete(playlist.id)
            failures: list[str] = []
            try:
                schedule_store.forget_playlist(playlist.id)
            except Exception as error:
                failures.append(f"schedule rules: {error}")
            try:
                display_store.forget_playlist(playlist.id)
            except Exception as error:
                failures.append(f"display assignments: {error}")
            saved_settings: config.Settings | None = None
            try:
                saved_settings = config.forget_playlist_default(playlist.id)
            except config.ConfigError as error:
                failures.append(f"saved default: {error}")
            return _PlaylistDeleteResult(
                playlist.id,
                playlist.name,
                settings=saved_settings,
                failures=tuple(failures),
            )

        return work

    def _adopt_playlist_delete(self, result: _PlaylistDeleteResult) -> tuple[str, ...]:
        """Reconcile every portion which committed, even after partial cleanup."""
        failures = list(result.failures)
        if result.settings is not None:
            previous = self._settings
            with self._settings_authoring_lock:
                self._settings_requested = result.settings
            self._adopt_settings(previous, result.settings)
        manual = self._session.manual_playlist == result.playlist_id
        self.playlists_changed()
        if manual:
            try:
                self._session.resume_schedule()
            except ApplyError as error:
                failures.append(f"current manual override: {error}")
            if not self.resume_schedule_async():
                failures.append("current manual override could not be released yet")
        return tuple(failures)

    def delete_playlist_async(
        self,
        reference: str,
        on_complete: Callable[[_PlaylistDeleteResult, tuple[str, ...]], None],
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> bool:
        """Delete one playlist and its cross-store references away from GTK."""

        def finish(result: _PlaylistDeleteResult) -> None:
            on_complete(result, self._adopt_playlist_delete(result))

        return self.authoring_action_async(
            lambda: _PlaylistDeleteResult("", ""),
            finish,
            prepare=lambda: self._prepare_playlist_delete(reference),
            failure=on_error,
        )

    def runtime_config_changed(self) -> None:
        """Publish a light authoring change that does not require a rescan."""
        self._publish_runtime_for_context()

    def activate_playlist(self, reference: str) -> Response:
        """Switch immediately to a named playlist and apply its first entry."""
        try:
            chosen = self._session.playlists.find(reference)
        except playlists.PlaylistError as error:
            return Response.failure(str(error))
        try:
            response = client.send_runtime("playlist-use", chosen.id)
        except client.NotRunningError:
            if not self._service_start:
                response = Response.failure("the wallpaper runtime is unavailable")
            else:
                try:
                    self._session.use_playlist(chosen.id)
                except ApplyError as error:
                    response = Response.failure(str(error))
                else:
                    response = self.apply(self._session.apply_current)
        except client.ControlError as error:
            response = Response.failure(str(error))
        if self._window is not None:
            self._window.playlists_changed(self._session)
        if response.ok:
            return Response.success(f"playing {chosen.name}")
        return response

    def activate_playlist_async(self, reference: str) -> bool:
        """Switch playlists from GTK without waiting on the runtime socket."""
        if self._gui_runtime_call_pending():
            return False
        try:
            chosen = self._session.playlists.find(reference)
        except playlists.PlaylistError as error:
            self.window_report(str(error))
            return False

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        return self._start_gui_runtime_call(
            lambda: self._send_gui_runtime("playlist-use", chosen.id),
            on_success=shown,
        )

    def activate_playlist_on_async(self, connector: str, reference: str) -> bool:
        """Switch one display without changing any other runtime route."""
        if self._gui_runtime_call_pending():
            return False
        try:
            chosen = self._session.playlists.find(reference)
        except playlists.PlaylistError as error:
            self.window_report(str(error))
            return False

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        return self._start_gui_runtime_call(
            lambda: self._send_gui_runtime_on(connector, "playlist-use", chosen.id),
            on_success=shown,
        )

    def resume_schedule(self) -> Response:
        """Release a manual playlist choice and apply the scheduled/default list."""
        try:
            response = client.send_runtime("schedule-follow")
        except client.NotRunningError:
            if not self._service_start:
                response = Response.failure("the wallpaper runtime is unavailable")
            else:
                try:
                    self._session.resume_schedule()
                except ApplyError as error:
                    response = Response.failure(str(error))
                else:
                    response = self.apply(self._session.apply_current)
        except client.ControlError as error:
            response = Response.failure(str(error))
        if self._window is not None:
            self._window.playlists_changed(self._session)
        return response

    def resume_schedule_async(self) -> bool:
        """Return to calendar control from GTK without blocking it."""
        if self._gui_runtime_call_pending():
            return False

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        return self._start_gui_runtime_call(
            lambda: self._send_gui_runtime("schedule-follow"),
            on_success=shown,
        )

    def resume_schedule_on_async(self, connector: str) -> bool:
        """Drop one display's manual override without touching the others."""
        if self._gui_runtime_call_pending():
            return False

        def shown(_runtime_answered: bool) -> None:
            if self._window is not None:
                self._window.playlists_changed(self._session)

        return self._start_gui_runtime_call(
            lambda: self._send_gui_runtime_on(connector, "schedule-follow"),
            on_success=shown,
        )

    def schedule_edited(self) -> None:
        """Take a changed calendar into account now rather than at the next tick."""
        self._publish_runtime_for_context()
        if self._window is not None:
            self.refresh_runtime_status_async()
            return
        if not self._service_start or self._runtime_is_running():
            return
        self._apply_changed_schedule()

    def _apply_changed_schedule(self) -> None:
        """Compatibility fallback after a definitive missing runtime socket."""
        if self._session.schedule_changed():
            self.apply(self._session.apply_current)
        if self._window is not None:
            self._window.show_current(self._session)

    def pairing_changed(self, item: MediaItem) -> None:
        """Make the window agree after a pairing moved over the socket.

        Only the wallpaper on screen is re-applied: changing the colours of
        something nobody is looking at would be a surprise. The rescan is
        deferred for the reason every other one here is -- it is the window's
        work, not the client's, and `ctl palette` should not be held open
        while the library is walked.
        """
        session = self._session
        self._publish_runtime_for_context()
        cursor = session.cursor
        if self._window is not None:
            if cursor is not None and cursor.path == item.path:
                self.refresh_runtime_status_async()
        elif (
            self._service_start
            and not self._runtime_is_running()
            and cursor is not None
            and cursor.path == item.path
        ):
            GLib.idle_add(self._reapply_current)
        GLib.idle_add(self.refresh_library)

    def _reapply_current(self) -> bool:
        if self._service_start:
            self.apply(self._session.apply_current)
        return GLib.SOURCE_REMOVE

    def apply(self, action: Callable[[], Applied]) -> Response:
        """Run a navigation action and report it, without letting it kill the app."""
        try:
            applied = action()
        except ApplyError as error:
            return Response.failure(str(error))
        if self._window is not None:
            # Only the highlight moves: rebuilding the grid on every `next`
            # would throw away every loaded thumbnail and flicker.
            self._window.show_current(self._session)
        return Response.success(applied.describe())

    # -- browsing from the control socket --------------------------------

    def browse_off_thread(self, work: Callable[[Browser], Response]) -> server.Deferred:
        """Run ``work`` against a `Browser` on a worker, and answer when it lands.

        The control server answers from the GTK main loop, so running a search
        or a download where it is called would stop the app drawing for as long
        as the website takes -- seconds, or minutes for a video. Nothing about
        the socket makes that necessary: the client is waiting on a reply, not
        on this thread, so the work goes to a worker and the reply is written
        when it comes back through `GLib.idle_add`. That is the arrangement
        `ui.browse_dialog` already uses for the same two calls, and this is the
        second user of it rather than a second design.

        The connection stays open in the meantime, which costs one file
        descriptor and keeps `ctl search` an ordinary blocking command that
        prints its results.
        """

        def start(reply: Callable[[Response], None]) -> None:
            # Built here rather than in the worker: `Settings.roots` is
            # main-thread state, and this is the thread that owns it.
            browser = Browser(
                root=download_root(self._settings),
                library_roots=self._settings.roots,
            )
            with self._browse_lock:
                if self._browse_shutdown:
                    browser.shutdown()
                    reply(Response.failure("application is shutting down"))
                    return
                self._browse_transports.add(browser)
            try:
                future = self._browse_pool().submit(work, browser)
            except RuntimeError:
                self._finish_browse_transport(browser)
                reply(Response.failure("application is shutting down"))
                return
            future.add_done_callback(lambda done: self._deliver(done, reply, browser))

        return server.Deferred(start=start)

    def _browse_pool(self) -> ThreadPoolExecutor:
        """The workers browsing verbs run on, made on first use.

        One worker, so two `ctl` invocations arriving together queue instead of
        fighting over each provider's request spacing. Made lazily because most
        runs of this app never touch a provider at all.
        """
        if self._browse_jobs is None:
            self._browse_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-browse")
        return self._browse_jobs

    def _finish_browse_transport(self, browser: Browser) -> None:
        with self._browse_lock:
            self._browse_transports.discard(browser)
        browser.shutdown()

    def _deliver(
        self,
        future: Future[Response],
        reply: Callable[[Response], None],
        browser: Browser,
    ) -> None:
        """Carry a worker's answer back to the main thread. Runs off it."""
        self._finish_browse_transport(browser)
        try:
            response = future.result()
        except Exception as error:
            # Broad on purpose: an unreachable network raises whatever the
            # transport underneath felt like, and none of it may reach the
            # client as a traceback or take the app down with it. `server.failed`
            # keeps a ProviderError's machine-readable kind.
            response = server.failed(error)

        def deliver() -> bool:
            with self._browse_lock:
                stopped = self._browse_shutdown
            if not stopped:
                reply(response)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(deliver)

    # -- cycle timer -----------------------------------------------------

    def _stop_cycle(self) -> None:
        if self._cycle_source:
            GLib.source_remove(self._cycle_source)
            self._cycle_source = 0

    def sync_cycle_timer(self) -> None:
        """Start, stop, or re-time the cycle timer to match the settings."""
        self._stop_cycle()
        if not self._service_start or not self._settings.cycle_enabled:
            return
        self._cycle_source = GLib.timeout_add_seconds(
            self._settings.cycle_interval, self._on_cycle_tick
        )

    def _on_cycle_tick(self) -> bool:
        if self._runtime_is_running():
            return GLib.SOURCE_CONTINUE
        # A failure here is nearly always an empty library or a missing file;
        # neither is a reason to stop cycling, so keep the timer alive.
        self.apply(self._session.next)
        return GLib.SOURCE_CONTINUE

    def _start_schedule_timer(self) -> None:
        """Re-read the calendar every so often, independently of cycling.

        Its own timer rather than the cycle one, because a schedule has to take
        effect whether or not the wallpaper is rotating -- somebody with cycling
        off and a "weekends" rule still expects Saturday to look different.

        `SCHEDULE_TICK_SECONDS` is a minute because that is the resolution the
        rules are written at; checking more often cannot notice anything sooner
        and checking less often would let a rule start late by up to its own
        error.
        """
        if not self._service_start:
            return
        self._schedule_source = GLib.timeout_add_seconds(
            SCHEDULE_TICK_SECONDS, self._on_schedule_tick
        )

    def _on_schedule_tick(self) -> bool:
        if self._runtime_is_running():
            return GLib.SOURCE_CONTINUE
        # Only redraws when the calendar actually asks for a different
        # playlist, so a quiet minute costs one comparison.
        if self._session.schedule_changed():
            # A scheduled playlist switch is a playback event, not merely a
            # cursor rebuild. This runs in service mode too, where no window
            # exists to accidentally make the transition happen later.
            self.apply(self._session.apply_current)
            if self._window is not None:
                self._window.show_library(self._session)
        return GLib.SOURCE_CONTINUE

    # -- control socket --------------------------------------------------

    def _start_control_socket(self) -> None:
        control = server.SocketServer(_Commands(self))
        try:
            control.start()
        except RuntimeError as error:
            # Losing the control socket costs the plugin's buttons, not the
            # app. Say so and carry on.
            print(f"warning: control socket unavailable: {error}", file=sys.stderr)
            return
        self._control = control

    @property
    def settings(self) -> config.Settings:
        return self._settings

    @property
    def requested_settings(self) -> config.Settings:
        """Latest accepted GUI intent, including a coalesced pending write."""
        with self._settings_authoring_lock:
            return self._settings_requested

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool:
        """Persist a GUI settings edit without waiting for locks or fsync.

        Notifications which arrive while a write is queued are folded into
        the same semantic field map.  A change arriving during the actual
        write becomes one trailing actor task, so GTK never emits one fsync per
        spin-row tick and unrelated edits made by another process survive.
        """
        if not self.require_authoring_ready():
            if on_error is not None:
                on_error(self._migration_blocked_response().message)
            return False
        try:
            with self._settings_authoring_lock:
                requested = replace(self._settings_requested, **changes).validated()
                self._settings_authoring_generation += 1
                generation = self._settings_authoring_generation
                self._settings_authoring_pending.update(
                    {key: (generation, value) for key, value in changes.items()}
                )
                self._settings_authoring_callbacks.append((generation, on_success, on_error))
                self._settings_requested = requested
                if self._settings_authoring_running:
                    return True
                self._settings_authoring_running = True
        except (TypeError, ValueError) as error:
            message = str(error) or "invalid settings"
            if on_error is not None:
                on_error(message)
            else:
                self.window_report(message)
            return False
        return self._start_settings_authoring()

    def _start_settings_authoring(self) -> bool:
        return self.authoring_action_async(
            self._persist_settings_batch,
            self._finish_settings_batch,
            failure=self._fail_settings_batch,
            # A newer field was already accepted while the current batch was
            # in flight.  Explicit quit refuses new gestures but must drain
            # this coalesced tail before releasing the application.
            allow_during_quit=True,
            # Source-changing settings take the runtime/removal lane before
            # the migration marker below.  Letting the generic actor acquire
            # those locks in the opposite order would deadlock a compiler
            # which already owns the runtime lane and is waiting for the
            # marker.
            guard_migration_transaction=False,
        )

    def _persist_settings_batch(self) -> _SettingsResult:
        with self._settings_authoring_lock:
            generation = self._settings_authoring_generation
            changes = tuple(
                (key, field_generation, value)
                for key, (field_generation, value) in self._settings_authoring_pending.items()
            )

        def persist() -> _SettingsResult:
            if not changes:  # pragma: no cover - guarded by the GTK scheduler
                return _SettingsResult(generation, config.load_strict(), ())
            change_map = {key: value for key, _field_generation, value in changes}
            wanted_default = change_map.get("active_playlist")
            if (
                isinstance(wanted_default, str)
                and wanted_default
                and playlists.Store.open().get(wanted_default) is None
            ):
                raise config.ConfigError(
                    "the chosen default playlist was deleted before Settings could save"
                )
            saved = config.update(change_map)
            return _SettingsResult(generation, saved, changes)

        source_change = any(key in {"roots", "scan_workshop"} for key, _gen, _value in changes)
        if source_change:
            # A compiler/reload owns this same lane for its entire snapshot to
            # Rust acknowledgement.  A new durable source set cannot land in
            # the middle and cause that older immutable Library to be blessed
            # after the ensuing scan fails.  Lock order is deliberately the
            # same as removal: runtime lane, migration marker, store locks.
            with (
                self._runtime_removal_lock,
                legacy_migration.unattended_transaction(),
            ):
                return persist()
        with legacy_migration.unattended_transaction():
            return persist()

    def _finish_settings_batch(self, result: _SettingsResult) -> None:
        """Adopt exactly the durable generation and queue at most one tail."""
        callbacks: list[
            tuple[
                int,
                Callable[[config.Settings], None] | None,
                Callable[[str], None] | None,
            ]
        ] = []
        with self._settings_authoring_lock:
            for key, field_generation, _value in result.changes:
                pending_field = self._settings_authoring_pending.get(key)
                if pending_field is not None and pending_field[0] <= field_generation:
                    self._settings_authoring_pending.pop(key, None)
            callbacks = [
                callback
                for callback in self._settings_authoring_callbacks
                if callback[0] <= result.generation
            ]
            self._settings_authoring_callbacks = [
                callback
                for callback in self._settings_authoring_callbacks
                if callback[0] > result.generation
            ]
            pending = bool(self._settings_authoring_pending)
            self._settings_authoring_running = pending
            self._settings_requested = replace(
                result.settings,
                **{
                    key: value
                    for key, (_field_generation, value) in self._settings_authoring_pending.items()
                },
            ).validated()
        previous = self._settings
        self._adopt_settings(previous, result.settings)
        for _generation, success, _failure in callbacks:
            if success is not None:
                success(result.settings)
        if pending:
            self._start_settings_authoring()

    def _fail_settings_batch(self, message: str) -> None:
        """Roll every coalesced control back to the last durable snapshot."""
        with self._settings_authoring_lock:
            callbacks = self._settings_authoring_callbacks
            self._settings_authoring_callbacks = []
            self._settings_authoring_pending.clear()
            self._settings_authoring_running = False
            self._settings_requested = self._settings
        if self._window is not None:
            self._window.apply_settings(self._settings)
        delivered = False
        for _generation, _success, failure in callbacks:
            if failure is not None:
                failure(message)
                delivered = True
        if not delivered:
            self.window_report(f"Settings were not saved; nothing changed: {message}")

    def update_settings(self, **changes: Any) -> config.Settings:
        previous = self._settings
        # Explicit legacy-service and non-GUI callers retain a synchronous
        # boundary.  They still rebase semantic fields so a stale process
        # cannot erase unrelated settings authored elsewhere.
        candidate = (
            self.legacy_authoring_sync(lambda: config.update(changes))
            if self.legacy_service
            else config.update(changes)
        )
        self._settings_requested = candidate
        self._adopt_settings(previous, candidate)
        return candidate

    def _adopt_settings(
        self,
        previous: config.Settings,
        candidate: config.Settings,
    ) -> None:
        """Reconcile one already-durable immutable snapshot on GTK."""
        library_sources_changed = (candidate.roots, candidate.scan_workshop) != (
            previous.roots,
            previous.scan_workshop,
        )
        async_scan = library_sources_changed and self._window is not None
        # Persist before adoption. A failed write must leave the application,
        # Session, runtime document and visible controls on the same last-known
        # durable snapshot.
        self._settings = candidate
        self._session.update_settings(candidate, rescan_library=not async_scan)
        if library_sources_changed and not async_scan:
            self._session.sync_with_noctalia()
        # Do not publish a temporary document which combines new roots with an
        # old library. The completed async scan publishes the coherent pair.
        if not async_scan:
            self._publish_runtime_for_context()
        self.sync_cycle_timer()
        if self._resolved is not None:
            self._apply_stylesheet(self._resolved)
        if self._window is not None:
            self._window.apply_settings(candidate)
            if candidate.dynamics_enabled != previous.dynamics_enabled:
                # Dynamics changes which wallpapers are playable at all, so the
                # grid has different contents now, not just a different state.
                self._window.show_library(self._session)
            elif not async_scan:
                self._window.show_current(self._session)
        if async_scan:
            self.refresh_library()
        elif library_sources_changed:
            self._make_missing_stills()
        if self._suppress_palette_reload == 0 and (
            self._settings.preview_scheme != previous.preview_scheme
            or self._settings.follow_noctalia_palette != previous.follow_noctalia_palette
        ):
            self.reload_palette()


class _Commands:
    """Control-socket verb implementations.

    Thin on purpose: each verb is one call into the authoring session plus a
    sentence describing what happened. Runtime controls go directly to the
    Rust socket; this socket retains library, pairing, playlist, schedule and
    provider authoring. Browsing and live-palette reloads answer later rather
    than waiting for a website or Noctalia on GTK's thread.

    What is thin here is not the same as easy. Every verb below that takes a
    path hands it to `control.server` to be resolved against the library before
    anything happens to a file, and every one that changes the library or the
    favourites leaves through `Application.forget` or
    `Application.favourites_changed`, so the running window never disagrees
    with what the socket just did.
    """

    def __init__(self, application: Application) -> None:
        self._app = application

    def _legacy_authoring(
        self,
        work: Callable[[], _AuthoringResult],
    ) -> _AuthoringResult:
        """Use the migration transaction while retaining tiny test doubles."""
        guarded = getattr(self._app, "legacy_authoring_sync", None)
        return guarded(work) if guarded is not None else work()

    def authoring_gate(self) -> Response | None:
        """Central profile-mutation admission used by the verb table."""
        # Small protocol/session test doubles predate the application-wide
        # migration lifecycle and represent an explicitly admitted profile.
        if getattr(self._app, "authoring_ready", True):
            settings_busy = bool(getattr(self._app, "_settings_authoring_running", False))
            if (
                getattr(self._app, "_authoring_active", False)
                or bool(getattr(self._app, "_authoring_queue", ()))
                or settings_busy
            ):
                return Response.failure(
                    "another authoring change is still being saved; retry after it finishes",
                    kind="authoring-busy",
                )
            return None
        blocked = getattr(self._app, "_migration_blocked_response", None)
        return blocked() if callable(blocked) else None

    def next_wallpaper(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        return self._app.apply(self._app.session.next)

    def previous_wallpaper(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        return self._app.apply(self._app.session.previous)

    def random_wallpaper(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        return self._app.apply(self._app.session.random)

    def _adopt_control_settings(
        self,
        updated: config.Settings,
        describe: Callable[[config.Settings], str],
    ) -> Response:
        previous = self._app.settings
        with self._app._settings_authoring_lock:
            requested = replace(
                updated,
                **{
                    key: value
                    for key, (_generation, value) in (self._app._settings_authoring_pending.items())
                },
            ).validated()
            self._app._settings_requested = requested
        self._app._adopt_settings(previous, updated)
        if requested != updated and self._app._window is not None:
            # The control mutation is durable, but a newer GUI gesture is
            # already accepted and queued. Keep those optimistic controls in
            # place instead of visibly bouncing back until its fsync lands.
            self._app._window.apply_settings(requested)
        return Response.success(describe(updated))

    def set_shuffle(self, value: str | None) -> server.Outcome:
        def work() -> config.Settings:
            return config.mutate(
                lambda current: replace(
                    current,
                    shuffle=server.parse_toggle(value, current.shuffle),
                )
            )

        return self._app.authoring_off_thread(
            work,
            lambda updated: self._adopt_control_settings(
                updated,
                lambda saved: f"shuffle {'on' if saved.shuffle else 'off'}",
            ),
        )

    def set_cycle(self, value: str | None) -> server.Outcome:
        def work() -> config.Settings:
            return config.mutate(
                lambda current: replace(
                    current,
                    cycle_enabled=server.parse_toggle(value, current.cycle_enabled),
                )
            )

        return self._app.authoring_off_thread(
            work,
            lambda updated: self._adopt_control_settings(
                updated,
                lambda saved: f"cycle {'on' if saved.cycle_enabled else 'off'}",
            ),
        )

    def set_cycle_interval(self, value: str | None) -> server.Outcome:
        if value is None:
            return Response.success(f"cycle-interval {self._app.settings.cycle_interval}")
        try:
            seconds = int(value)
        except ValueError:
            return Response.failure(f"expected a whole number of seconds, got {value!r}")
        return self._app.authoring_off_thread(
            lambda: config.update({"cycle_interval": seconds}),
            lambda updated: self._adopt_control_settings(
                updated,
                lambda saved: f"cycle-interval {saved.cycle_interval}",
            ),
        )

    def set_dynamics(self, value: str | None) -> server.Outcome:
        def work() -> config.Settings:
            return config.mutate(
                lambda current: replace(
                    current,
                    dynamics_enabled=server.parse_toggle(value, current.dynamics_enabled),
                )
            )

        return self._app.authoring_off_thread(
            work,
            lambda updated: self._adopt_control_settings(
                updated,
                lambda saved: f"dynamics {'on' if saved.dynamics_enabled else 'off'}",
            ),
        )

    def reload_palette(self) -> server.Outcome:
        """Let the post-hook wait for the worker without stopping GTK."""

        def start(reply: server.Reply) -> None:
            def finished(resolved: source.ResolvedPalette | None, error: str) -> None:
                if resolved is None:
                    reply(Response.failure(f"palette reload failed: {error or 'unknown error'}"))
                else:
                    reply(Response.success(f"palette reloaded ({resolved.origin.value})"))

            self._app.reload_palette(finished)

        reload_action = server.Deferred(start=start)
        if self._app.legacy_service:
            return reload_action
        return self._app.authoring_barrier(lambda: reload_action)

    def open_page(self, value: str | None) -> Response:
        """Present the configuration window on one named workflow page."""
        requested = (value or "").strip().casefold()
        aliases = {
            "browse": "browse",
            "media": "media",
            # Compatibility for callers from before pairings became the
            # per-item editor reached from Media/Pairings.
            "pairings": "media",
            "playlists": "playlists",
            "schedules": "schedules",
            "displays": "schedules",
            "settings": "settings",
        }
        page = aliases.get(requested)
        if page is None:
            return Response.failure(
                "usage: open <browse|media|pairings|playlists|schedules|displays|settings>"
            )
        self._app.present_page(page)
        return Response.success(f"opened {page}")

    def report_status(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        return Response.success(self._app.session.describe())

    # -- the library ------------------------------------------------------

    def list_library(self, value: str | None) -> Response:
        """What is in the library, as rows a script can read.

        The scan the window is already showing, not a fresh one: `ctl list`
        answers what the app currently believes, which is the same thing the
        grid is drawing. `ctl status` reports the counts and the refresh button
        re-reads the disk.
        """
        kinds, text = server.parse_list(value)
        session = self._app.session
        return Response.success(
            server.render_library(
                session.library.items,
                library_filter.Query(text=text, kinds=kinds),
                session.favourites.paths,
            )
        )

    def select_wallpaper(self, value: str | None) -> server.Outcome:
        if self._app.legacy_service:
            # The explicit headless compatibility service intentionally owns
            # its Python renderer and retains the synchronous path.
            session = self._app.session
            item = server.resolve(session.library, value, verb="select")
            health = session.pairings.health(pairings.Identity.of(item))
            if health.is_borked:
                return Response.failure(
                    f"Playback unavailable for {item.name}; "
                    "see Library for details and removal options"
                )
            return self._app.play_item(item)

        def prepare() -> Callable[[], tuple[MediaItem, playlists.Playlist]]:
            session = self._app.session
            item = server.resolve(session.library, value, verb="select")
            health = session.pairings.health(pairings.Identity.of(item))
            if health.is_borked:
                raise ValueError(
                    f"Playback unavailable for {item.name}; "
                    "see Library for details and removal options"
                )
            playlist_store = session.playlists

            def work() -> tuple[MediaItem, playlists.Playlist]:
                chosen = playlist_store.set_singleton(
                    QUICK_CHOICE_ID,
                    QUICK_CHOICE_NAME,
                    item.path,
                )
                return item, chosen

            return work

        def finish(result: tuple[MediaItem, playlists.Playlist]) -> server.Outcome:
            _item, chosen = result
            # The Store transaction is complete; only the pure Session cursor
            # reconciliation and visible model update happen on GTK.
            self._app.session.use_playlist(chosen.id)
            self._app.playlists_changed()
            return self._app.control_runtime_off_thread("playlist-use", chosen.id)

        def unprepared() -> tuple[MediaItem, playlists.Playlist]:  # pragma: no cover
            raise RuntimeError("select was not prepared")

        return self._app.authoring_off_thread(
            unprepared,
            finish,
            prepare=prepare,
        )

    def list_favourites(self) -> Response:
        session = self._app.session
        return Response.success(
            server.render_favourites(
                session.favourites.favourites.entries,
                tuple(item.path for item in session.library.items),
            )
        )

    def show_pairing(self, value: str | None) -> Response:
        """What one wallpaper resolves to: still, motion, palette, mode."""
        session = self._app.session
        item = server.resolve(session.library, value, verb="pairing")
        bundle = session.pairings.resolve_accepted(item, session.library)
        return Response.success(server.describe_pairing(item, bundle))

    def set_still(self, value: str | None) -> server.Outcome:
        """`still <wallpaper> <picture>`, or `default` to stop choosing.

        The wallpaper is resolved against the library, so a record can only
        name something the scan produced. A manually chosen picture is resolved
        the same way: it is a first-class still with its own metadata, not an
        untracked attachment to the moving wallpaper.
        """
        source, chosen = server.parse_pair(value, verb="still")
        if self._app.legacy_service:
            session = self._app.session
            item = server.resolve(session.library, source, verb="still")
            still: Path | None = None
            if chosen != "default":
                requested = server.parse_path(chosen, verb="still")
                still = session.library.require_representative_still(requested).path
            self._legacy_authoring(lambda: session.pairings.choose_still(item, still))
            self._app.pairing_changed(item)
            return Response.success(
                f"{item.name} uses {still.name}"
                if still
                else f"{item.name} works its own still out"
            )

        def prepare() -> Callable[[], _StillPairingMutationResult]:
            session = self._app.session
            item = server.resolve(session.library, source, verb="still")
            still: Path | None = None
            if chosen != "default":
                requested = server.parse_path(chosen, verb="still")
                still = session.library.require_representative_still(requested).path
            return self._app.prepare_still_pairing_mutation(
                item,
                still,
                lambda store, current, current_still: store.choose_still(
                    current,
                    current_still,
                ),
            )

        def finish(result: _StillPairingMutationResult) -> Response:
            item = self._app.adopt_pairing_still(result.item, result.effective_still)
            still = result.record.still
            self._app.pairing_changed(item)
            return Response.success(
                f"{item.name} uses {still.name}"
                if still
                else f"{item.name} works its own still out"
            )

        def unprepared() -> _StillPairingMutationResult:  # pragma: no cover
            raise RuntimeError("still pairing was not prepared")

        return self._app.authoring_off_thread(
            unprepared,
            finish,
            prepare=prepare,
        )

    def set_palette(self, value: str | None) -> server.Outcome:
        """`palette <wallpaper> <policy>` -- adaptive, keep, or `source:name`."""
        source, encoded = server.parse_pair(value, verb="palette")
        policy = pairings.PalettePolicy.decode(encoded)
        if policy.encode() != encoded:
            raise ValueError(f"not a palette policy: {encoded}")
        if self._app.legacy_service:
            session = self._app.session
            item = server.resolve(session.library, source, verb="palette")
            self._legacy_authoring(lambda: session.pairings.choose_palette(item, policy))
            self._app.pairing_changed(item)
            return Response.success(f"{item.name} asks for {policy.encode()}")

        def prepare() -> Callable[[], tuple[MediaItem, pairings.Pairing]]:
            session = self._app.session
            item = server.resolve(session.library, source, verb="palette")
            pairing_store = session.pairings

            def work() -> tuple[MediaItem, pairings.Pairing]:
                return item, pairing_store.choose_palette(item, policy)

            return work

        def finish(result: tuple[MediaItem, pairings.Pairing]) -> Response:
            item, _record = result
            self._app.pairing_changed(item)
            return Response.success(f"{item.name} asks for {policy.encode()}")

        def unprepared() -> tuple[MediaItem, pairings.Pairing]:  # pragma: no cover
            raise RuntimeError("palette pairing was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def reset_pairing(self, value: str | None) -> server.Outcome:
        """Forget every choice made for one wallpaper."""
        if self._app.legacy_service:
            session = self._app.session
            item = server.resolve(session.library, value, verb="reset-pairing")
            if not self._legacy_authoring(lambda: session.pairings.reset(item)):
                return Response.success(f"{item.name} had nothing customized")
            self._app.pairing_changed(item)
            return Response.success(f"{item.name} is back to its defaults")

        def prepare() -> Callable[[], _PairingResetMutationResult]:
            session = self._app.session
            item = server.resolve(session.library, value, verb="reset-pairing")
            return self._app.prepare_pairing_reset(item)

        def finish(result: _PairingResetMutationResult) -> Response:
            item = self._app.adopt_pairing_still(result.item, result.effective_still)
            if not result.changed:
                return Response.success(f"{item.name} had nothing customized")
            self._app.pairing_changed(item)
            return Response.success(f"{item.name} is back to its defaults")

        def unprepared() -> _PairingResetMutationResult:  # pragma: no cover
            raise RuntimeError("pairing reset was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def list_playlists(self, value: str | None) -> Response:
        """Every playlist, or the entries of one named playlist."""
        session = self._app.session
        if not (value or "").strip():
            return Response.success(
                server.describe_playlists(session.playlists, session.active_playlist())
            )
        playlist = session.playlists.find(value or "")
        return Response.success(server.describe_playlist(playlist, session.library))

    def make_playlist(self, value: str | None) -> server.Outcome:
        session = self._app.session
        if self._app.legacy_service:
            made = self._legacy_authoring(lambda: session.playlists.create(value or ""))
            self._app.runtime_config_changed()
            return Response.success(f"made {made.name}")
        playlist_store = session.playlists

        def finish(made: playlists.Playlist) -> Response:
            self._app.playlists_changed()
            return Response.success(f"made {made.name}")

        return self._app.authoring_off_thread(
            lambda: playlist_store.create(value or ""),
            finish,
        )

    def drop_playlist(self, value: str | None) -> server.Outcome:
        reference = value or ""

        if self._app.legacy_service and not hasattr(self._app, "_prepare_playlist_delete"):
            # Compatibility for the deliberately tiny headless Session test
            # double.  A real Application takes the structured path below.
            session = self._app.session
            playlist = session.playlists.find(reference)

            def delete_compatibility_playlist() -> None:
                session.playlists.delete(playlist.id)
                session.schedules.forget_playlist(playlist.id)
                session.displays.forget_playlist(playlist.id)

            self._legacy_authoring(delete_compatibility_playlist)
            self._app.playlists_changed()
            return Response.success(f"deleted {playlist.name}")

        def finish(result: _PlaylistDeleteResult) -> Response:
            failures = self._app._adopt_playlist_delete(result)
            if failures:
                return Response.failure(
                    f"deleted {result.name}, but cleanup needs attention: " + "; ".join(failures),
                    kind="partial-cleanup",
                )
            return Response.success(f"deleted {result.name}")

        if self._app.legacy_service:
            try:
                result = self._legacy_authoring(
                    lambda: self._app._prepare_playlist_delete(reference)()
                )
                return finish(result)
            except Exception as error:
                return server.failed(error)
        return self._app.authoring_off_thread(
            lambda: _PlaylistDeleteResult("", ""),
            finish,
            prepare=lambda: self._app._prepare_playlist_delete(reference),
        )

    def list_displays(self) -> server.Outcome:
        """`displays` -- discover screens away from GTK and answer later."""
        session = self._app.session
        # Assignments store the playlist *id*, so a rename does not break them.
        # Nobody reads ids, so they are resolved back to names here rather than
        # printed raw. Capture only immutable authoring values; the worker must
        # never receive the live Session.
        names = tuple((playlist.id, playlist.name) for playlist in session.playlists.all())
        assignments = session.displays.all()

        def work() -> Response:
            attached = outputs.discover()
            resolved_names = dict(names)

            def shown(identifier: str) -> str:
                # A name that no longer resolves means the playlist went
                # without taking its assignment -- say so, not a bare hash.
                return resolved_names.get(identifier) or f"{identifier} (missing)"

            lines = ["# fields: connector, playlist, description"]
            assigned = dict(assignments)
            connected = {screen.name for screen in attached}
            for screen in attached:
                wanted = assigned.get(screen.name)
                lines.append(
                    f"{screen.name}\t{shown(wanted) if wanted else '(default)'}\t{screen.label}"
                )
            # Saved dock assignments stay visible while disconnected.
            for connector, playlist in assignments:
                if connector not in connected:
                    lines.append(f"{connector}\t{shown(playlist)}\t(not attached)")
            if not attached and not assigned:
                return Response.success("# no screens reported; wallpapers apply everywhere")
            return Response.success("\n".join(lines))

        return self._app.runtime_off_thread(work)

    def assign_display(self, value: str | None) -> server.Outcome:
        """`display-assign <connector> <playlist>`.

        Split from the left: a connector never contains a space and a playlist
        name may.
        """
        connector, _, wanted = (value or "").strip().partition(" ")
        if not connector or not wanted.strip():
            return Response.failure("usage: display-assign <connector> <playlist>")
        if self._app.legacy_service:
            session = self._app.session
            playlist = session.playlists.find(wanted.strip())
            self._legacy_authoring(lambda: session.displays.assign(connector, playlist.id))
            self._app.runtime_config_changed()
            return Response.success(f"{connector} shows {playlist.name}")

        def prepare() -> Callable[[], playlists.Playlist]:
            session = self._app.session
            playlist = session.playlists.find(wanted.strip())
            display_store = session.displays

            def work() -> playlists.Playlist:
                display_store.assign(connector, playlist.id)
                return playlist

            return work

        def finish(playlist: playlists.Playlist) -> Response:
            self._app.runtime_config_changed()
            return Response.success(f"{connector} shows {playlist.name}")

        def unprepared() -> playlists.Playlist:  # pragma: no cover
            raise RuntimeError("display assignment was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def clear_display(self, value: str | None) -> server.Outcome:
        """`display-clear <connector>` -- follow the default again."""
        connector = (value or "").strip()
        if not connector:
            return Response.failure("usage: display-clear <connector>")
        session = self._app.session
        if self._app.legacy_service:
            if not self._legacy_authoring(lambda: session.displays.unassign(connector)):
                return Response.failure(f"{connector} was already following the default")
            self._app.runtime_config_changed()
            return Response.success(f"{connector} follows the default again")
        display_store = session.displays

        def finish(changed: bool) -> Response:
            if not changed:
                return Response.failure(f"{connector} was already following the default")
            self._app.runtime_config_changed()
            return Response.success(f"{connector} follows the default again")

        return self._app.authoring_off_thread(
            lambda: display_store.unassign(connector),
            finish,
        )

    def add_to_playlist(self, value: str | None) -> server.Outcome:
        """`playlist-add <playlist> <wallpaper>`.

        Split from the left here, not the right: the playlist name is the
        short side and the wallpaper is the path. A name with a space in it
        therefore has to be referred to by id, which `playlists` prints.
        """
        name, source = server.parse_pair_from_left(value, verb="playlist-add")
        if self._app.legacy_service:
            session = self._app.session
            playlist = session.playlists.find(name)
            item = server.resolve(session.library, source, verb="playlist-add")
            self._legacy_authoring(lambda: session.playlists.add(playlist.id, item.path))
            self._app.playlists_changed()
            return Response.success(f"{item.name} added to {playlist.name}")

        def prepare() -> Callable[[], tuple[playlists.Playlist, MediaItem]]:
            session = self._app.session
            playlist = session.playlists.find(name)
            item = server.resolve(session.library, source, verb="playlist-add")
            playlist_store = session.playlists

            def work() -> tuple[playlists.Playlist, MediaItem]:
                playlist_store.add(playlist.id, item.path)
                return playlist, item

            return work

        def finish(result: tuple[playlists.Playlist, MediaItem]) -> Response:
            playlist, item = result
            self._app.playlists_changed()
            return Response.success(f"{item.name} added to {playlist.name}")

        def unprepared() -> tuple[playlists.Playlist, MediaItem]:  # pragma: no cover
            raise RuntimeError("playlist addition was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def remove_from_playlist(self, value: str | None) -> server.Outcome:
        """`playlist-remove <playlist> <entry-id>`, as `playlists <name>` prints."""
        name, entry = server.parse_pair_from_left(value, verb="playlist-remove")
        if self._app.legacy_service:
            session = self._app.session
            playlist = session.playlists.find(name)
            self._legacy_authoring(lambda: session.playlists.remove_entry(playlist.id, entry))
            self._app.playlists_changed()
            return Response.success(f"removed {entry} from {playlist.name}")

        def prepare() -> Callable[[], playlists.Playlist]:
            session = self._app.session
            playlist = session.playlists.find(name)
            playlist_store = session.playlists

            def work() -> playlists.Playlist:
                playlist_store.remove_entry(playlist.id, entry)
                return playlist

            return work

        def finish(playlist: playlists.Playlist) -> Response:
            self._app.playlists_changed()
            return Response.success(f"removed {entry} from {playlist.name}")

        def unprepared() -> playlists.Playlist:  # pragma: no cover
            raise RuntimeError("playlist removal was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def use_playlist(self, value: str | None) -> server.Outcome:
        """Play one playlist now, or ``none`` to resume calendar control."""
        wanted = (value or "").strip()
        if wanted.casefold() in ("", "none"):
            if self._app.legacy_service:
                response = self._app.resume_schedule()
                if not response.ok:
                    return response
                return Response.success("following the display schedule")
            return self._app.authoring_off_thread(
                lambda: None,
                lambda _nothing: self._app.control_runtime_off_thread(
                    "schedule-follow",
                    success=lambda _response: Response.success("following the display schedule"),
                    on_success=self._refresh_playlist_window,
                ),
            )
        if self._app.legacy_service:
            playlist = self._app.session.playlists.find(wanted)
            return self._app.activate_playlist(playlist.id)

        def prepare() -> Callable[[], playlists.Playlist]:
            playlist = self._app.session.playlists.find(wanted)
            return lambda: playlist

        def finish(playlist: playlists.Playlist) -> server.Outcome:
            return self._app.control_runtime_off_thread(
                "playlist-use",
                playlist.id,
                success=lambda _response: Response.success(f"playing {playlist.name}"),
                on_success=self._refresh_playlist_window,
            )

        def unprepared() -> playlists.Playlist:  # pragma: no cover
            raise RuntimeError("playlist use was not prepared")

        return self._app.authoring_off_thread(
            unprepared,
            finish,
            prepare=prepare,
        )

    def _refresh_playlist_window(self) -> None:
        """Reconcile the authoring view after a runtime-owned mode change."""
        if self._app._window is not None:
            self._app._window.playlists_changed(self._app.session)

    def show_schedule(self) -> Response:
        session = self._app.session
        return Response.success(
            schedules.describe(
                session.schedules.rules,
                session.settings.active_playlist,
                datetime.now(),
                {one.id: one.name for one in session.playlists.all()},
            )
        )

    def add_schedule_rule(self, value: str | None) -> server.Outcome:
        """Append a rule. Appending is how you override: the last match wins."""
        name, options = server.parse_rule(value)
        months = tuple(part for part in options.get("months", "").split(",") if part)
        weekdays = tuple(part for part in options.get("days", "").split(",") if part)
        start = options.get("from", "")
        end = options.get("to", "")

        def work_for(playlist: playlists.Playlist) -> schedules.Rule:
            return self._app.session.schedules.add(
                playlist.id,
                months=months,
                weekdays=weekdays,
                start=start,
                end=end,
            )

        def finish(result: tuple[playlists.Playlist, schedules.Rule]) -> Response:
            playlist, rule = result
            self._app.schedule_edited()
            return Response.success(f"{playlist.name} scheduled: {rule.describe()}")

        if self._app.legacy_service:
            playlist = self._app.session.playlists.find(name)
            rule = self._legacy_authoring(lambda: work_for(playlist))
            return finish((playlist, rule))

        def prepare() -> Callable[[], tuple[playlists.Playlist, schedules.Rule]]:
            playlist = self._app.session.playlists.find(name)
            schedule_store = self._app.session.schedules

            def work() -> tuple[playlists.Playlist, schedules.Rule]:
                rule = schedule_store.add(
                    playlist.id,
                    months=months,
                    weekdays=weekdays,
                    start=start,
                    end=end,
                )
                return playlist, rule

            return work

        def unprepared() -> tuple[playlists.Playlist, schedules.Rule]:  # pragma: no cover
            raise RuntimeError("schedule rule was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def drop_schedule_rule(self, value: str | None) -> server.Outcome:
        session = self._app.session
        rule_id = (value or "").strip()

        def finish(changed: bool) -> Response:
            if not changed:
                raise ValueError(f"no schedule rule {rule_id}")
            self._app.schedule_edited()
            return Response.success(f"removed rule {rule_id}")

        if self._app.legacy_service:
            return finish(self._legacy_authoring(lambda: session.schedules.remove(rule_id)))
        schedule_store = session.schedules
        return self._app.authoring_off_thread(
            lambda: schedule_store.remove(rule_id),
            finish,
        )

    def add_favourite(self, value: str | None) -> server.Outcome:
        """Star a wallpaper the library knows about.

        Resolved against the library, so the state file can only ever fill with
        paths the scan produced. A star on something we cannot see would be a
        line in a file with no tile, no rotation entry and nothing to take it
        off again.
        """
        if self._app.legacy_service:
            item = server.resolve(self._app.session.library, value, verb="favourite")
            return self._star(item.path, wanted=True)

        def prepare() -> Callable[[], tuple[Path, bool]]:
            item = server.resolve(self._app.session.library, value, verb="favourite")
            store = self._app.session.favourites
            return lambda: (item.path, store.add(item.path))

        def finish(result: tuple[Path, bool]) -> Response:
            path, moved = result
            self._app.favourites_changed()
            return Response.success(
                f"{path.name} starred" if moved else f"{path.name} was already starred"
            )

        def unprepared() -> tuple[Path, bool]:  # pragma: no cover
            raise RuntimeError("favourite was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def remove_favourite(self, value: str | None) -> server.Outcome:
        """Unstar a path, whether or not the library still has it.

        The asymmetry with `favourite` is deliberate and is the whole reason
        this one does not resolve. `library.favourites` keeps an entry whose
        file has gone -- an unmounted drive, a root taken out of the settings
        -- precisely so the list is not silently pruned, and taking the star
        off by hand is the only thing left to do with one. Resolving here would
        make the entries you most want to remove the ones you cannot.
        """
        return self._star(server.parse_path(value, verb="unfavourite"), wanted=False)

    def _star(self, path: Path, *, wanted: bool) -> server.Outcome:
        """Move one star, and leave the window agreeing with the store.

        The session's store, never a new one: the window's tiles and the
        rotation are built from that object, and a second copy here would mean
        `ctl favourite` and the star on the tile disagreeing until the next
        launch.
        """
        store = self._app.session.favourites

        def work() -> bool:
            return store.add(path) if wanted else store.discard(path)

        def finish(moved: bool) -> Response:
            self._app.favourites_changed()
            if wanted:
                return Response.success(
                    f"{path.name} starred" if moved else f"{path.name} was already starred"
                )
            return Response.success(
                f"{path.name} unstarred" if moved else f"{path.name} was not starred"
            )

        if self._app.legacy_service:
            try:
                return finish(self._legacy_authoring(work))
            except favourites.FavouritesError as error:
                # Store mutations are transactional: a failed write also
                # leaves memory unchanged, so nothing is redrawn.
                return server.failed(error)
        return self._app.authoring_off_thread(work, finish)

    def remove_wallpaper(self, value: str | None) -> server.Outcome:
        """Delete a downloaded wallpaper, or trash one of the user's own.

        There is no confirmation dialogue on a socket, so the refusals in
        `library.manage` are the whole of the protection and nothing here may
        weaken them: the path is resolved against the library first, so a
        string naming no wallpaper never reaches `manage` at all, ownership is
        re-derived from disk there, and there is no flag that turns any of it
        off. A failure arrives carrying the `kind` that says which refusal it
        was -- `not-ours` for the user's own file, `missing` for one already
        gone.
        """
        session = self._app.session
        if self._app.legacy_service:

            def remove_legacy() -> Response:
                item = server.resolve(session.library, value, verb="remove")
                try:
                    intent = self._app.prepare_item_removal(item)
                except removals.RemovalJournalError as error:
                    return server.failed(error)
                source_context: file_io.PinnedDirectoryContext | None = None
                try:
                    try:
                        source_context = intent.pin_source_context()
                    except (OSError, ValueError) as error:
                        cancellation = self._app.cancel_item_removal(intent)
                        detail = (
                            "; the prepared removal intent could not be cleared: "
                            f"{'; '.join(cancellation)}"
                            if cancellation
                            else ""
                        )
                        return Response.failure(
                            "could not retain the prepared source directory for "
                            f"{item.path}: {error}{detail}",
                            kind="local-io",
                        )
                    try:
                        source_pin = session.removal_journal.source_pin(intent)
                    except removals.RemovalJournalError as error:
                        cancellation = self._app.cancel_item_removal(intent)
                        detail = (
                            "; the prepared removal intent could not be cleared: "
                            f"{'; '.join(cancellation)}"
                            if cancellation
                            else ""
                        )
                        return Response.failure(str(error) + detail, kind=error.kind)

                    anchored_source = source_context.child(intent.path.name)
                    try:
                        message = server.remove_wallpaper(
                            item,
                            session.library.roots,
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
                                session.retain_removal(intent)
                                return Response.failure(str(error), kind=error.kind)
                            cancellation = self._app.cancel_item_removal(intent)
                            detail = (
                                "; the file was not removed, but its prepared removal intent "
                                f"could not be cleared: {'; '.join(cancellation)}"
                                if cancellation
                                else ""
                            )
                            return Response.failure(str(error) + detail, kind=error.kind)
                        failures = self._app.forget_item(
                            item,
                            intent=intent,
                            artifacts_already_clean=True,
                            artifact_source_root=intent.source_root,
                            artifact_lookup_root=source_context.root_anchor,
                            artifact_lookup_parent=source_context.directory_anchor,
                            artifact_source_context=source_context,
                        )
                        return Response.failure(
                            str(error) + manage.metadata_cleanup_note(failures),
                            kind=error.kind,
                        )
                    failures = self._app.forget_item(
                        item,
                        intent=intent,
                        artifacts_already_clean=True,
                        artifact_source_root=intent.source_root,
                        artifact_lookup_root=source_context.root_anchor,
                        artifact_lookup_parent=source_context.directory_anchor,
                        artifact_source_context=source_context,
                    )
                    if failures:
                        return Response.failure(
                            message + manage.metadata_cleanup_note(failures),
                            kind="metadata-cleanup",
                        )
                    return Response.success(message)
                finally:
                    if source_context is not None:
                        source_context.close()

            def guarded_remove_legacy() -> Response:
                with runtime_config.compiler_lock():
                    return remove_legacy()

            return self._legacy_authoring(guarded_remove_legacy)

        def prepare() -> Callable[[], RemovalResult]:
            if self._app._removal_active or self._app._removal_convergence_held:
                raise removals.RemovalJournalError(
                    "busy", "another wallpaper removal or cleanup refresh is still in progress"
                )
            item = server.resolve(self._app.session.library, value, verb="remove")
            plan = self._app.session.prepare_removal_plan(item, trash=not item.deletable)
            self._app._invalidate_runtime_for_pending_removal()
            self._app._removal_active = True

            def run() -> RemovalResult:
                with (
                    self._app._runtime_removal_lock,
                    legacy_migration.unattended_transaction(),
                    runtime_config.compiler_lock(),
                ):
                    return plan.run()

            return run

        def finish(result: RemovalResult) -> Response:
            self._app._adopt_item_removal(result)
            metadata = manage.metadata_cleanup_note(result.cleanup_failures)
            if not result.committed:
                detail = (
                    "; the file was not removed, but its prepared removal intent "
                    f"could not be cleared: {'; '.join(result.cancellation_failures)}"
                    if result.cancellation_failures
                    else ""
                )
                return Response.failure(
                    result.error_message + detail,
                    kind=result.error_kind or "local-io",
                )
            if result.error_message:
                return Response.failure(
                    result.error_message + metadata,
                    kind=result.error_kind or "local-io",
                )
            physical = result.physical
            if isinstance(physical, manage.Removal):
                message = (
                    f"{physical.describe()} - deleted, which cannot be undone"
                    f"{physical.cleanup_note()}"
                )
            elif isinstance(physical, manage.Trashed):
                message = (
                    f"{result.item.path.name} moved to the trash - {physical.destination}"
                    f"{physical.cleanup_note()}"
                )
            else:  # pragma: no cover - a committed success always has its result
                return Response.failure("removal completed without a physical result")
            if result.cleanup_failures:
                return Response.failure(message + metadata, kind="metadata-cleanup")
            return Response.success(message)

        def unprepared() -> RemovalResult:  # pragma: no cover - prepare always replaces it
            raise RuntimeError("removal transaction was not prepared")

        return self._app.authoring_off_thread(
            unprepared,
            finish,
            prepare=prepare,
            finalize=self._app._release_removal_lane,
            guard_migration_transaction=False,
        )

    # -- browsing ---------------------------------------------------------

    def list_providers(self) -> Response:
        """Every provider and what it cannot currently do.

        The one browsing verb that answers on the spot: the registry reads the
        environment and a small key file, and touches no network at all.
        """
        return Response.success(server.render_providers(registry.describe()))

    def search(self, value: str | None) -> server.Outcome:
        name, text = server.parse_search(value)

        def work(browser: Browser) -> Response:
            # One page, the first: the protocol carries a single argument, and
            # spending it on a page number would cost the query the spaces in it.
            result = browser.search(name, SearchQuery(text=text))
            return Response.success(server.render_search(result))

        return self._app.browse_off_thread(work)

    def download(self, value: str | None) -> server.Outcome:
        name, identifier, variant = server.parse_download(value)

        def work(browser: Browser) -> Response:
            # Both providers re-fetch their own detail page from the identifier
            # and take nothing else off the candidate, so a search result is not
            # needed to name one -- which is what makes `search` and `download`
            # usable as two separate commands. Everything about where the bytes
            # land, the directory marker and the sidecar is the provider's, the
            # same code the download button runs.
            provider = browser.provider(name)
            candidate = WallpaperCandidate(
                provider=name,
                identifier=identifier,
                title="",
                kind=provider.media_kind,
                # Old MotionBGS sidecars predate their provider id field and
                # are indexed by this canonical source page.  Supplying it lets
                # the pre-network duplicate check protect migrated downloads
                # just like cards returned by a GUI search.
                page_url=source_page_url(name, identifier),
            )
            done = browser.download(candidate, variant=variant)
            # The file is in the library directory but not in the library until
            # something looks again. Back on the main thread to do it, since the
            # scan ends in the grid.
            GLib.idle_add(self._app.refresh_library)
            return Response.success(f"{done.describe()} -> {done.result.path}")

        return self._app.browse_off_thread(work)

    def quit(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        GLib.idle_add(self._app.request_quit)
        return Response.success("quitting")


def run(
    argv: list[str] | None = None,
    *,
    service: bool = False,
    initial_page: str | None = None,
) -> int:
    # GtkApplication overwrites prgname with the application id on Wayland, so
    # setting it here would be cosmetic at best and misleading at worst. The
    # Wayland app-id comes from paths.APPLICATION_ID; see docs/niri.md.
    GLib.set_application_name("Wall-in-One")
    application = Application(service=service, initial_page=initial_page)
    try:
        application.register(None)
    except GLib.Error as error:
        application._stills.shutdown()
        application._session.shutdown()
        print(
            f"Cannot register Wall-in-One with the desktop session: {error.message}",
            file=sys.stderr,
        )
        return 1
    if application.get_is_remote():
        # Registration, not a pre-launch socket-existence check, resolves the
        # actual GApplication owner. Inspect its exported immutable identity
        # before run() can forward activation to a different package. Old
        # releases have no action, so their compatibility is unknown, not true.
        identity = (
            application.get_action_state(PACKAGE_SOURCE_ACTION)
            if application.has_action(PACKAGE_SOURCE_ACTION)
            else None
        )
        verified = (
            identity is not None
            and identity.get_type_string() == "s"
            and identity.unpack() == PACKAGE_SOURCE
            and application.has_action(PRESENT_PACKAGE_ACTION)
        )
        # Remote instances never ran do_startup and own no background workers.
        # Release only resources allocated by this unstarted local shell.
        application._stills.shutdown()
        application._session.shutdown()
        if not verified:
            from wall_in_one.ui import update_prompt

            print(update_prompt.DESCRIPTION, file=sys.stderr)
            if service:
                return 75
            return update_prompt.run(application.activate)
        if not service:
            application.activate_action(
                PRESENT_PACKAGE_ACTION,
                GLib.Variant("(ss)", (PACKAGE_SOURCE, initial_page or "")),
            )
            connection = application.get_dbus_connection()
            if connection is not None:
                try:
                    connection.flush_sync(None)
                except GLib.Error as error:
                    print(
                        f"Cannot confirm the request reached the running app: {error.message}",
                        file=sys.stderr,
                    )
                    return 1
        return 0
    return application.run(argv if argv is not None else [])
