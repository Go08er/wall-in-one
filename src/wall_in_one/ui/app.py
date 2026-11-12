"""Application object: wires the palette, the stylesheet, and the control socket."""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import re
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent import futures
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from wall_in_one import (
    config,
    dry_run,
    file_io,
    legacy_migration,
    paths,
    runtime_compatibility,
    runtime_config,
    runtime_health,
)
from wall_in_one.browse import Browser, source_page_url
from wall_in_one.control import client, server
from wall_in_one.control.protocol import Response
from wall_in_one.library import (
    displays,
    favourites,
    manage,
    pairing,
    pairings,
    playlists,
    removals,
    scan,
    schedules,
    state_file,
    stills,
    workshop,
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
    RemovalPlan,
    RemovalResult,
    Session,
    removal_lock_refusal,
)
from wall_in_one.theme import css, noctalia, source
from wall_in_one.ui import dial_rows, monitors, quit_countdown, runtime_truth
from wall_in_one.ui.activity_log import ActivityLog
from wall_in_one.ui.next.real_playlists import RenameResult
from wall_in_one.ui.next.real_schedule import ColorsForgotten
from wall_in_one.ui.next.window import NextWindow
from wall_in_one.ui.setup_section import SetupLane
from wall_in_one.ui.status_model import RuntimeStatusModel, RuntimeStatusView, StatusChange
from wall_in_one.ui.stills import StillMaker
from wall_in_one.ui.tidy_section import TidyLane
from wall_in_one.ui.window import ACCELERATORS, MainWindow
from wall_in_one.ui.window_services import DEFAULT_UI, FirstRunHost, UiKind, WindowServices
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


def _unfinished_replay(failures: Sequence[str]) -> str:
    """What a pending removal's replayed cleanup couldn't finish, said to the window."""
    return (
        "Pending removal cleanup could not finish: "
        + "; ".join(failures[:3])
        + ". Fix the state-directory problem and refresh to retry."
    )


def _left_by_replay(left: Sequence[str]) -> str:
    """What a pending removal's replay finished and left in place on purpose, said once.

    Not `_unfinished_replay`: nothing failed and nothing is retried. The
    removal is finished, its pending record is gone, and a later scan has
    nothing more to say of it (`Session.removal_replay_left`).
    """
    omitted = len(left) - 3
    more = f"; and {omitted} more" if omitted > 0 else ""
    return "A pending removal was finished. " + "; ".join(left[:3]) + more + "."


def _unfinished_library_cleanup(result: LibraryRefreshResult) -> str:
    """Everything one reconciliation couldn't clean up or left on purpose, as one message, or empty.

    For a reconciliation a newer scan superseded: its cleanups are adopted
    without its Library, and nobody else says what it couldn't finish. The
    current one says the same through `_finish_library_refresh`.
    """
    said = []
    if result.replay_failures:
        said.append(_unfinished_replay(result.replay_failures))
    if result.replay_left:
        said.append(_left_by_replay(result.replay_left))
    if result.workshop_cleanup_failures:
        said.append(
            "Workshop uninstall" + manage.metadata_cleanup_note(result.workshop_cleanup_failures)
        )
    return " ".join(said)


#: A caller's claim that a failed task changed nothing ("...; nothing changed:
#: error"), the one wording every window caller uses for it.
_NOTHING_CHANGED: Final = re.compile(r"[;,] nothing changed(?=[:.]|$)")


def _with_durability(message: str, warnings: Sequence[str]) -> str:
    """``message``, then each warning it doesn't already say, as sentences of one message."""
    said = [message.rstrip(" .")] if message.strip() else []
    said.extend(warning.rstrip(" .") for warning in warnings if warning not in message)
    return ". ".join(said) + "." if said else ""


def _runtime_installed(
    settings: config.Settings, session: Session
) -> tuple[bool, tuple[runtime_config.LeftOut, ...], str | None]:
    """`runtime_config.publish_changes`: whether a file changed, what was left out and replaced.

    The last is the ``runtime.toml`` text the new one replaced (`Published.replaced`),
    None when it didn't replace one: what the service still holds if it refuses the
    new one (`Application._web_off_guard`).

    What was left out is the compiled document's, whether or not a byte
    changed: every reference to a playlist that can't play, which the
    compiler builds without (`runtime_config.LeftOut`). The lane hands it to
    GTK, which says it when it changes (`Application._note_left_out`).

    A pair published but not confirmed durable is installed.
    `runtime_config.RuntimeConfigNotDurableError` comes only after the whole
    new pair was renamed into place, so the service's watcher already reads
    it. Treated as a failure, the lane neither reloaded the service nor
    counted the document, and the next update found nothing to change, so
    only the watcher ever applied it. It is a change: reload it, and report
    the uncertainty (`state_file.report_not_durable`). Callers check the
    inventory (`Application._inventory_installable`) before this, as before.
    """
    try:
        published = runtime_config.publish_changes(settings, session)
    except runtime_config.RuntimeConfigNotDurableError as uncertain:
        state_file.report_not_durable(str(uncertain))
        changes = uncertain.changes
        if changes is None:
            return True, (), None
        return True, changes.left_out, getattr(changes, "replaced", None)
    return bool(published), published.left_out, getattr(published, "replaced", None)


#: How many replaced ``runtime.toml`` texts are kept as what the service may
#: still hold (`Application._web_off_guard`); past it, one isn't known.
HELD_CANDIDATES: Final = 4

LOGGER = logging.getLogger(__name__)


def _save_in_order(works: Sequence[Callable[[], Any]]) -> None:
    """Run each write handed on at shutdown, one after another: renderer numbers, then Settings.

    On the authoring worker, after the write it was queued behind
    (`Application._shutdown_authoring_jobs`). One that fails is logged and the
    rest still run: nobody is left to tell.
    """
    for work in works:
        try:
            work()
        except Exception:
            LOGGER.exception("a setting handed on at shutdown could not be saved")


def _settings_published[Saved: (config.Settings, config.Settings | None)](
    write: Callable[[], Saved],
) -> Saved:
    """Run one settings write; one that published but couldn't sync its folder saved.

    The file already holds the new settings (`config.SettingsNotDurableError`),
    so they are what is returned, read back, and the uncertainty is reported
    (`state_file.report_not_durable`), never "nothing changed". The window's
    own batch does the same in `Application._persist_settings_batch`.
    """
    try:
        return write()
    except config.SettingsNotDurableError as error:
        state_file.report_not_durable(str(error))
        try:
            return config.load_strict()
        except config.ConfigError:
            return error.settings


def _playlist_namer(listing: playlists.Store, playlist_id: str) -> Callable[[str], bool]:
    """Whether a reference names the playlist ``playlist_id``, as the compiler resolves one.

    By id first, then by name, case-insensitively and trimmed
    (`runtime_config.find_playlist`): an older profile's default, rule or
    display assignment may name its playlist by name, in any letter case.
    ``listing`` is the playlists as just read from their file on the worker,
    before the playlist is deleted or renamed. The test this returns is then
    put to each reference as it is read under its own store's lock, so what
    another program saved since a window opened is judged too.
    """

    def names(reference: str) -> bool:
        if not reference or runtime_config.is_fallback_reference(reference):
            # The compiler resolves the generated playlist first, so a
            # hand-edited authored playlist of that name never owns the reference.
            return False
        found = listing.get(reference)
        if found is None:
            found = listing.by_name(reference)
        return found is not None and found.id == playlist_id

    return names


#: How many times a change to the library sources is judged again because
#: ``settings.toml`` moved on while it was being judged.
SOURCE_SAVE_ATTEMPTS: Final = 5


def _save_checked_sources(
    change_map: Mapping[str, Any],
    roots_edits: Sequence[config.RootsEdit],
    check: Callable[[config.Settings, config.Settings], None],
) -> config.Settings:
    """Save a change to the library sources, judged against what is saved now.

    The settings are read fresh and each folder gesture is applied to the
    folders they hold (never a list from a window's snapshot: another program
    may have saved folders of its own since). ``check``, the dry run, judges
    exactly that candidate, and it is written only if the file still holds
    what was read (``judged_against``, compared under the settings lock).
    When another writer got in between, all of it is done again on what they
    saved.
    """
    for _attempt in range(SOURCE_SAVE_ATTEMPTS):
        durable = config.load_strict()
        resolved = dict(change_map)
        if roots_edits:
            resolved["roots"] = config.edited_roots(durable.roots, roots_edits)
        check(durable, replace(durable, **resolved).validated())
        try:
            return config.update(resolved, judged_against=durable)
        except config.SettingsMovedOnError:
            continue
    raise config.SettingsBusyError(
        "settings.toml kept changing while this change was being checked"
    )


#: How often the calendar is re-read. A minute, because that is the
#: resolution schedule rules are written at.
SCHEDULE_TICK_SECONDS: Final = 60
RUNTIME_STATUS_TICK_SECONDS: Final = 2
PALETTE_RELOAD_DEBOUNCE_MS: Final = 75
MAX_GUI_AUTHORING_QUEUE: Final = 32
#: How long a shutdown that wasn't asked for through `Application.request_quit`
#: (a bare ``quit()``) keeps running the loop for the numbers it hands on to
#: be saved, and published: as long as ``ctl`` gives one authoring change.
SHUTDOWN_SETTLE_SECONDS: Final = client.AUTHORING_TIMEOUT
#: How long a first Ctrl-C holds the changes still waiting to be saved before it
#: saves them and quits, so that a second can quit without them, or any other key cancel
#: (`Application._first_ctrl_c`); counted down in steps of `QUIT_COUNTDOWN_TICK_MS`.
QUIT_COUNTDOWN_SECONDS: Final = 5
QUIT_COUNTDOWN_TICK_MS: Final = 1000
#: What a second Ctrl-C during that countdown says as it quits.
QUIT_WITHOUT_THEM: Final = "Quitting immediately without them."
#: What a key during that countdown says when ``ctl quit`` asked to quit meanwhile.
QUIT_GOES_ON: Final = "Quitting goes on: wall-in-one ctl quit asked for it."
#: The longest a settings save can take, from the timeouts it can spend: the
#: migration marker's lock it is written under (`legacy_migration.unattended_transaction`),
#: then the settings file's own lock, and under that the check of the service that
#: "Stop animations on battery" makes (`runtime_compatibility.require_schema`), whose
#: status call and check of the loaded units share one `client.TIMEOUT`. What the
#: last step of a shutdown waits for a save still being written, at most
#: (`Application._web_off_on_disk_at_exit`).
SETTINGS_SAVE_SECONDS: Final = (
    legacy_migration.MIGRATION_LOCK_TIMEOUT_SECONDS
    + state_file.MUTATION_LOCK_TIMEOUT_SECONDS
    + client.TIMEOUT
)
#: How long that step waits for the compiler lock: briefly, since a publication
#: holding it reads the setting under it (review r13c, D2), and what is left is
#: judged once it has settled (`Application._web_off_judged_once_landed`).
SHUTDOWN_WEB_OFF_LOCK_SECONDS: Final = 2.0
#: How long that later judgement waits for the compiler lock, once the save and
#: this process's runtime lane are done: as any writer of the file waits for it.
SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS: Final = runtime_config.COMPILER_LOCK_TIMEOUT_SECONDS
#: That judgement's thread's name.
WEB_OFF_AT_EXIT_THREAD: Final = "wio-web-off-at-exit"
#: The name of the daemon threads that step waits for only as long as it may
#: (`_unless_given_up`): a parse, a look at Workshop items, a ``quit``.
SHUTDOWN_LOOK_THREAD: Final = "wio-shutdown-look"
#: The longest that step gives a wallpaper service older than schema 8 to answer
#: ``quit`` (`Application._quit_older_service`): its own timeout, and the wait for it.
SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS: Final = 1.0
#: How long that step looks at the files it judges, at most, before the lock
#: and under it together (`_ExitLook`): a hand edit's parses, and, for a service
#: with no check of an item's type of its own, the Workshop items a file names
#: (`_web_named`). Past it, a parse not finished counts as allowing web
#: wallpapers, or as naming what can't be checked, and the items it hasn't
#: looked at count as web pages.
SHUTDOWN_WORKSHOP_LOOK_SECONDS: Final = 2.0
#: How often a wait in that step looks whether a second Ctrl-C has ended it.
SHUTDOWN_INTERRUPT_POLL_SECONDS: Final = 0.1
#: What Regenerate says when there are library folders but none a still can go in
#: (`pairing.library_capture_root`, f7 G4).
NO_STILLS_FOLDER: Final = (
    "No still can be made: none of your library folders can take Wall-in-One\u2019s "
    "stills (each is not there, is or lies in a Workshop item\u2019s folder, or reaches its "
    "Wall-in-One folder through a link). Add a folder of your own in Settings \u2192 Library"
)
#: How long a removal waits, at most, for web wallpapers going off to reach the
#: service before it takes the runtime lane (`Application._removal_run`).
REMOVAL_WEB_OFF_WAIT_SECONDS: Final = 10.0
#: The line the compiler writes in ``[settings]`` while it allows web wallpapers
#: (`runtime_config._document_head`). Nothing else it writes starts a line with
#: the key: every value is one quoted line after its own key.
_COMPILED_WEB_ALLOWED: Final = re.compile(r"^allow_web_wallpapers = true$", re.MULTILINE)


def _unless_given_up[Judged](
    work: Callable[[], Judged],
    give_up: Callable[[], bool],
    cut: Judged,
    *,
    until: float | None = None,
) -> Judged:
    """``work()``, run on a thread of its own and waited for in slices; ``cut`` once ``give_up``.

    For what the end of a shutdown can't bound, such as a parse of a large
    document: a second Ctrl-C (``give_up``) ends the wait, and Python runs its
    handler between the slices (`SHUTDOWN_INTERRUPT_POLL_SECONDS`). ``until``
    (a `time.monotonic` deadline) ends it too, never later: the last slice is
    cut to it. The thread is a daemon, so the process doesn't wait for work it
    stopped waiting for. What ``work`` raises is raised here, or, once the
    wait was cut, logged: nothing reads it then.
    """
    if give_up() or (until is not None and until <= time.monotonic()):
        return cut  # already cut: not even started
    done: Future[Judged] = Future()

    def unread(finished: Future[Judged]) -> None:
        error = finished.exception()
        if error is not None:
            LOGGER.error(
                "what the end of a shutdown had stopped waiting for (a parse, a look at "
                "Workshop items, or a quit) then failed",
                exc_info=error,
            )

    def run() -> None:
        try:
            done.set_result(work())
        except BaseException as error:  # handed to the waiter
            done.set_exception(error)

    threading.Thread(target=run, name=SHUTDOWN_LOOK_THREAD, daemon=True).start()
    while not done.done():
        if give_up():
            done.add_done_callback(unread)
            return cut
        slice_ = SHUTDOWN_INTERRUPT_POLL_SECONDS
        if until is not None:
            left = until - time.monotonic()
            if left <= 0:
                done.add_done_callback(unread)
                return cut
            slice_ = min(slice_, left)
        futures.wait((done,), timeout=slice_)
    return done.result()


def _may_allow_web_wallpapers(
    text: str, give_up: Callable[[], bool] = lambda: False, until: float | None = None
) -> bool:
    """Whether ``text`` (a ``runtime.toml``) may allow web wallpapers, unparsed where it can be.

    One the compiler wrote has the key on a line of its own exactly when it
    allows them (`_COMPILED_WEB_ALLOWED`). One that can't hold the key at all
    (`runtime_config.may_spell_key`: not the name itself, nor a quoted key
    that spells it with escapes) doesn't: so a compiled document is never
    parsed, though a value in it (a still's path, a title) has a ``\\u`` in
    it (review r13f, N2: such a one was parsed, about 0.13 s for each
    megabyte, up to a second at the cap, with nothing to end it). Anything
    else, a hand edit, is parsed (`runtime_config.allows_web_wallpapers`,
    which counts one that isn't TOML 1.0 as allowing them), on a thread of its
    own (`_unless_given_up`): once ``give_up`` says so (a second Ctrl-C at the
    end of a shutdown), or ``until`` (the look's deadline) has passed, the
    wait for it ends, and the document counts as allowing them (fail closed).
    """
    if _COMPILED_WEB_ALLOWED.search(text) is not None:
        return True
    if not runtime_config.may_spell_key(text, "allow_web_wallpapers"):
        return False
    return _unless_given_up(
        lambda: runtime_config.allows_web_wallpapers(text=text), give_up, cut=True, until=until
    )


def _web_named(text: str, give_up: Callable[[], bool], until: float) -> tuple[bool, bool]:
    """Whether ``text`` names a Workshop web wallpaper, and whether it names what can't be checked.

    The ids it names are read off the compiler's own lines
    (`runtime_config.compiled_scenes`), or, from a hand edit, from a parse
    (`runtime_config.named_scenes`) on a thread of its own
    (`_unless_given_up`), whose wait ends as the look's does. Then each id's
    own ``project.json`` is read (`workshop.web_among`), and nothing else: no
    scan of the Workshop (review r13e, L1), so it takes as long as the file's
    ids make it. That look runs on a thread of its own too, Steam's list of
    libraries included, and the wait for it ends once ``give_up`` says so or
    ``until`` (the look's deadline) has passed, though one call of it is
    still slow (review r13g, S-L1: a library on a disk spinning up, or a hung
    mount). The first answer is what the files confirm. The second is every
    doubt: one that isn't TOML 1.0, or whose parse was cut, an id that can't
    be checked, and every id left once ``give_up`` says so or ``until`` has
    passed.
    """
    named = runtime_config.compiled_scenes(text)
    if named is None:
        named = _unless_given_up(
            lambda: runtime_config.named_scenes(text), give_up, cut=None, until=until
        )
        if named is None:
            return False, True
    if not named:
        return False, False
    ids = sorted(named)
    over = lambda: give_up() or time.monotonic() > until  # noqa: E731
    return _unless_given_up(
        lambda: workshop.web_among(ids, over), give_up, cut=(False, True), until=until
    )


#: Why the end of a shutdown replaces a file (`_may_play_web_while_off`).
_ALLOWS: Final = "allows"
_NAMES: Final = "names"
_NAMES_WEB: Final = "names-web"


def _may_play_web_while_off(
    text: str, status: object, give_up: Callable[[], bool], until: float
) -> str:
    """Why a service that loads ``text`` may play a web page while web wallpapers are off; or "".

    `_ALLOWS`: it may allow them, which any service obeys (`_may_allow_web_wallpapers`).
    Otherwise a service that reads schema 8 plays no page: it reads each
    item's type before it starts the engine. One older than that (``status``,
    the last one heard, lists no 8) has no such check: `_NAMES`, when the file
    names an item that can't be checked, which may be a web page
    (`_web_named`; review r13f, L2), and `_NAMES_WEB` when it names one the
    files confirm. With no status heard at all, only that counts (a preset
    whose base is one included): the
    service is then not running, and its start compiles again (or refuses
    with exit 78 a kept file that plays one), or it is current, and refuses a
    page at launch itself; so an item that can't be checked (unsubscribed, or
    on a library that isn't mounted) leaves the file as it is.

    Its parses and its look at the Workshop items share one deadline,
    ``until`` (`_ExitLook.until`).
    """
    if _may_allow_web_wallpapers(text, give_up, until):
        return _ALLOWS
    if runtime_config.nothing_to_play_supported(status):
        return ""
    confirmed, unchecked = _web_named(text, give_up, until)
    if confirmed:
        return _NAMES_WEB
    if unchecked and status is not None:
        return _NAMES
    return ""


def _log_restart_advice(why: str, look: _ExitLook, *, older: bool, quit_older: bool) -> None:
    """What to tell the user when the file that must be replaced is left as it is."""
    if older and not quit_older:  # no quit is sent: restarting is the remedy
        LOGGER.warning(
            "That service is older than this app, so it keeps playing what it "
            "holds until it is restarted: systemctl --user restart wall-in-one"
        )
    elif why == _ALLOWS and look.status is not None:  # a current service
        LOGGER.warning(
            "That service keeps the configuration it holds until it is restarted, "
            "and its start check then compiles that file again or refuses it: "
            "systemctl --user restart wall-in-one"
        )
    elif why == _ALLOWS:  # no service heard: it may or may not be running
        LOGGER.warning(
            "If the wallpaper service is running, it keeps the configuration it "
            "holds until it is restarted, and its start check then compiles that "
            "file again or refuses it: systemctl --user restart wall-in-one"
        )


@dataclass(slots=True)
class _ExitLook:
    """What the end of a shutdown judges a file by: the service last heard, and what it found.

    One look for the step's own judgements, before the lock and under it,
    which each text is judged once for (`reasons`); another for the later one.
    Each look has one deadline (`until`), `SHUTDOWN_WORKSHOP_LOOK_SECONDS`
    from its first judgement: a text judged again under the lock has only
    what is left of it.
    """

    status: object
    reasons: dict[str, str] = field(default_factory=dict)
    until: float | None = None

    def why(self, text: str, give_up: Callable[[], bool]) -> str:
        """`_may_play_web_while_off`, once for each text."""
        if text not in self.reasons:
            if self.until is None:
                self.until = time.monotonic() + SHUTDOWN_WORKSHOP_LOOK_SECONDS
            self.reasons[text] = _may_play_web_while_off(text, self.status, give_up, self.until)
        return self.reasons[text]


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
# A plain ``GLib.idle_add`` waits below GTK's redraw priority. While anything
# animates with frames that cost a refresh interval (a spinner, the entry
# cursor fade), GTK paints back to back and that idle never runs: the worker
# had the new palette but the window kept its old colours until the animation
# stopped. HIGH_IDLE is GLib's slot for work which must precede the next
# resize and redraw. One priority for the whole theme lane keeps its order.
THEME_DELIVERY_PRIORITY: Final = GLib.PRIORITY_HIGH_IDLE
# Every other worker hand-off that ends a busy state -- a library scan, an
# authoring transaction, a runtime or control reply, the rescan a playback
# gesture waits on after a removal -- runs at the priority of the socket and
# timer sources it answers, above redraw for the same reason. Each lane posts
# at this one priority, so each lane stays first in, first out.
COMPLETION_PRIORITY: Final = GLib.PRIORITY_DEFAULT
#: The reply kind for an authoring task whose write landed but whose adoption
#: on GTK, or what follows it, failed (`Application._saved_but_not_applied`).
SAVED_NOT_APPLIED: Final = "saved-not-applied"
_RuntimeResult = TypeVar("_RuntimeResult")
_AuthoringResult = TypeVar("_AuthoringResult")
_LibrarySources = tuple[tuple[Path, ...], bool, bool]
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


@dataclass(slots=True)
class _Said:
    """What one authoring task changed, and what the window has been told about it.

    One task, one message (`Application.with_task_durability`): what its
    caller says for it carries these, or the lane says them after the task
    when the caller says nothing.
    """

    #: What the work saved without confirming it durable (`state_file.report_not_durable`).
    warnings: list[str] = field(default_factory=list)
    #: Whether the window was told ``warnings``.
    said: bool = False
    #: Whether the work changed anything on disk (`state_file.record_write`): the
    #: only proof that a task which failed afterwards saved something.
    wrote: bool = False
    #: The work changed something, then failed: its caller's "nothing changed" isn't true.
    partly: bool = False
    #: Said in place of the caller's first report: a write landed, then its
    #: adoption failed (`Application._after_failed_finish`).
    instead: str = ""
    instead_said: bool = False


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
    #: A renderer number handed on (`dial_rows.handing_on`): by its own timer or a flush.
    #: What `Application._settle_before_shutdown` waits for, and what a shutdown still
    #: attempts when it never started (`Application._shutdown_authoring_jobs`).
    handed_on: bool = False
    #: A change the person made (`Application.authoring_action_async`'s callers but the
    #: application's own two, its library reconciliation and its runtime-health ingestion),
    #: not the application's own work: what a first Ctrl-C counts and offers to drop
    #: (`Application._changes_a_quit_would_drop`).
    edit: bool = False
    #: What the work saved but could not confirm durable (`state_file.report_not_durable`).
    spoken: _Said = field(default_factory=_Said, compare=False)


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
    #: The inventory stamp when ``library`` was captured (`Application._inventory_installable`).
    inventory: int


@dataclass(frozen=True, slots=True)
class _RuntimePublication:
    """One coalesced compiler/reload result delivered back to GTK."""

    generation: int
    changed: bool = False
    response: Response | None = None
    error: str = ""
    unavailable: bool = False
    deferred: bool = False
    #: Said after a refused reload's own words: what the web-off guard did.
    note: str = ""


class _RuntimeLibraryNotReadyError(Exception):
    """The accepted Library belongs to older source settings than disk."""


class _RuntimeInventoryInvalidError(_RuntimeLibraryNotReadyError):
    """The captured Library is not a valid inventory now: nothing may be installed from it."""


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
    #: The inventory stamp when ``library`` was captured; None: whatever is valid now.
    inventory: int | None = None


@dataclass(frozen=True, slots=True)
class _RuntimeHealthResult:
    """Durable health result whose Store ownership can move back to GTK."""

    request: _RuntimeHealthRequest
    changed: int = 0
    accepted: bool = False
    store: pairings.Store | None = None
    publication: _RuntimePublication | None = None
    error: str = ""
    #: A write to ``pairings.json`` landed in this transaction, whatever became
    #: of the publication after it. Not a save that found its markers already
    #: there: that one wrote nothing, so it proves nothing about the folder.
    saved: bool = False


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
    #: Set when the snapshot was published but its durability is unconfirmed.
    not_durable: str = ""


@dataclass(frozen=True, slots=True)
class _PlaylistDeleteResult:
    """A committed playlist deletion plus every independently durable tail."""

    playlist_id: str
    name: str
    settings: config.Settings | None = None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlaylistRenameResult:
    """A rename, and what it saved first so what names the playlist by name keeps working.

    ``renamed`` is None when a step failed after an earlier one was saved:
    ``saved`` says what was (each now names the playlist by its id, the same
    playlist) and ``error`` why it stopped. That is never "nothing changed";
    `Application.adopt_playlist_rename` says exactly this.
    """

    before: playlists.Playlist
    renamed: playlists.Playlist | None
    #: The settings saved first, when the default named it by name; else None.
    settings: config.Settings | None = None
    #: What was saved before the rename, for a sentence ("2 schedule rules").
    saved: tuple[str, ...] = ()
    error: str = ""


def rename_partly_saved(result: RenameResult) -> str:
    """What a rename that stopped partway saved, and why it stopped."""
    many = len(result.saved) > 1 or not result.saved[0].startswith(("the ", "1 "))
    return (
        f"\u201c{result.before.name}\u201d was not renamed: {result.error}. Saved before "
        f"that: {runtime_config.listed(result.saved)} now name{'' if many else 's'} it by its "
        "id instead of its name, which is the same playlist. Rename it again to finish."
    )


@dataclass(frozen=True, slots=True)
class _PlaylistReferenceRepairResult:
    """Idempotent cleanup of references left by an interrupted deletion."""

    settings: config.Settings | None = None
    schedule_playlists: tuple[str, ...] = ()
    display_playlists: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    #: References by name, now the playlist's id: (reference, id).
    schedule_retargets: tuple[tuple[str, str], ...] = ()
    display_retargets: tuple[tuple[str, str], ...] = ()


class Application(Adw.Application):
    """Owns app-wide state: settings, the live palette, and the CSS provider."""

    def __init__(
        self,
        *,
        service: bool = False,
        initial_page: str | None = None,
        ui: UiKind = DEFAULT_UI,
    ) -> None:
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
        # Which window every activation of this process builds. Fixed for the
        # process: a second launch with another --ui reaches this instance
        # and is shown this window (see `run`).
        self._ui: UiKind = ui
        self._held = False
        loaded_settings = config.load_document()
        self._settings = loaded_settings.settings
        # Keys in settings.toml this build does not know. Any settings write
        # would drop them, so while there are some the GUI is read-only for
        # settings: Preferences shows why and every write path refuses. This
        # is the startup snapshot; config's writers re-check the file itself.
        self._settings_unknown_keys = loaded_settings.unknown_keys
        # Typed by what the application uses, not by the classic window's
        # class, so any window implementing WindowServices can stand here.
        self._window: WindowServices | None = None
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
        #: Why the gate rests at ``repair`` when the repair itself never ran to
        #: its end (its lane's guard refused it): said with every refusal.
        self._authoring_gate_reason = ""
        #: Saved repairs whose adoption here failed, adopted again by the next.
        self._unadopted_repairs: tuple[_PlaylistReferenceRepairResult, ...] = ()
        self._headless_authoring_started = False
        self._activation_waiting_for_repair = False
        # Process work, done once: the first window to pass the migration gate
        # resolves the live palette and loads the library. Every later
        # activation only presents a window; one built later is shown what
        # the application already has and rescans the library once (see
        # `do_activate`).
        self._first_activation_done = False
        self._newer_version_notice_shown = False
        self._provider = Gtk.CssProvider()
        # The new window's glass dials; None keeps the classic stylesheet.
        self._window_glass: css.Glass | None = None
        self._control: server.SocketServer | None = None
        self._resolved: source.ResolvedPalette | None = None
        self._session = self._new_session(self._settings)
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
        #: The task the authoring worker has now, from the head of the queue
        #: until it completes (`_numbers_unsettled`).
        self._authoring_current: _AuthoringTask | None = None
        #: The worker's run of the latest task started, which the last step of
        #: a shutdown waits for, briefly, when it is a settings save
        #: (`_web_off_on_disk_at_exit`).
        self._authoring_future: Future[Any] | None = None
        #: A renderer number's write asked for the publication now held
        #: (`_runtime_publication_held`): the shutdown settle waits for it too.
        self._number_publication = False
        #: `do_shutdown` has begun: a publication goes to the runtime lane,
        #: whatever the mode, so that its socket calls can be cut off
        #: (`_publish_runtime_for_context`).
        self._shutting_down = False
        #: A second interrupt (Ctrl-C) ended the shutdown settle (`_settle_before_shutdown`).
        self._settle_cut = False
        #: The handler `run` installs for SIGINT while it runs (`_on_sigint`), or None.
        self._sigint_handler: Callable[[int, Any], None] | None = None
        #: A first Ctrl-C's countdown to saving what is still waiting (`_first_ctrl_c`).
        self._quit_countdown: quit_countdown.Countdown | None = None
        #: The authoring lane starts nothing new, and admits nothing new but what
        #: a requested quit admits: during that countdown, and once a second
        #: Ctrl-C has asked to quit without what is still waiting.
        self._authoring_held = False
        #: A second Ctrl-C asked to quit without the changes still waiting to be
        #: saved: the shutdown drops them instead of handing them on
        #: (`_quit_without_waiting_changes`).
        self._quit_without_waiting = False
        #: A first Ctrl-C has said the person's changes waiting are being saved,
        #: by the careful quit its countdown ended in, or by ``ctl quit``'s: the
        #: next quits without what is still waiting.
        self._ctrl_c_saving = False
        #: A Ctrl-C quit at once, or came once the shutdown had begun: ``run()``
        #: then ends as a Ctrl-C ends a GApplication's, with KeyboardInterrupt.
        self._ctrl_c_quit = False
        #: The runtime lane `do_shutdown` let go of, whose running job the process's
        #: exit waits for: the end of the shutdown's judgement once it is done
        #: (`_web_off_judged_once_landed`).
        self._runtime_lane_at_exit: ThreadPoolExecutor | None = None
        #: That judgement's thread, while one runs (tests join it).
        self._web_off_deferred: threading.Thread | None = None
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
        #: Folder gestures not yet saved, each with the generation that
        #: accepted it (`edit_roots_async`). The pending ``roots`` value is
        #: only what the window shows meanwhile: what is written is these,
        #: applied to the folders ``settings.toml`` holds at that moment.
        self._settings_roots_edits: list[tuple[int, config.RootsEdit]] = []
        self._settings_authoring_running = False
        self._settings_authoring_callbacks: list[
            tuple[
                int,
                Callable[[config.Settings], None] | None,
                Callable[[str], None] | None,
            ]
        ] = []
        #: The generation the batch at the head of the lane took
        #: (`_prepare_settings_batch`): what that batch owns and settles, and
        #: nothing accepted after it. None between batches.
        self._settings_batch_generation: int | None = None
        #: Whether that batch turns web wallpapers off: one still being written
        #: when the end of a shutdown stops waiting for it is judged once it
        #: lands (`_turn_off_in_flight`). Set with the generation.
        self._settings_batch_turns_web_off = False
        self._settings_requested = self._settings
        self._runtime_status_pending = False
        self._runtime_status_generation = -1
        self._runtime_status_again = False
        self._runtime_health_pending = False
        self._runtime_health_request: _RuntimeHealthRequest | None = None
        #: The health-save failure last said, and the window generation it was
        #: said to (`_say_health_failure_once`); None once a save lands.
        self._runtime_health_said: tuple[int, str] | None = None
        #: This session's log, in memory only: what the user was told and what
        #: the runtime's status said as it changed (the new interface shows it).
        self.activity_log = ActivityLog(folders=lambda: self._settings.roots)
        # The Rust snapshot is the sole playback truth for every GUI surface.
        # Keep the last valid answer through a transient deadline so the
        # header and authoring pages cannot briefly contradict one another.
        # Every runtime-status answer is published to this model and to
        # nothing else; the forwarding subscriber below hands each publication
        # to the window exactly as the call sites used to.
        self._status_model = RuntimeStatusModel()
        self._status_model.subscribe(self._forward_runtime_status)
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
        # Whether `session.library` is a valid inventory: it isn't from a
        # removal's prepare until it is known not to have committed, or until
        # a scan after its commit is accepted. Every installer checks it right
        # before `runtime_config.update` (`_inventory_installable`); the stamp
        # moves on every change, so a Library captured before one is never
        # installed after it.
        self._inventory_stamp = 0
        #: The durability sink this application replaced at startup (`_not_durable`).
        self._previous_durability_sink: Callable[[str], None] | None = None
        #: The authoring task whose result GTK is adopting now, so what its caller
        #: says carries what the task saved (`window_report`).
        self._speaking_for: _AuthoringTask | None = None
        self._inventory_valid = True
        self._inventory_problem = ""
        #: The inventory validity and problem a removal's prepare found in
        #: force, put back exactly when it doesn't commit
        #: (`_restore_runtime_after_uncommitted_removal`). Only the inventory:
        #: the scan lifecycle (pending, error, ready, generation) belongs to
        #: the scan worker, and a removal never saves or restores it.
        self._inventory_before_removal: tuple[bool, str] | None = None
        self._runtime_compile_pending = False
        self._runtime_authoring_request: _RuntimeAuthoringRequest | None = None
        #: Why the last publication didn't reach the wallpaper service, or empty.
        self._publication_problem = ""
        #: What the web-off guard last did on a lane (`_explaining_web_off_guard`),
        #: kept apart from the problem until a publication gets through, and
        #: whether it has been said: the publication that carried it may be
        #: replaced before it is (R42-22). Written under `_runtime_config_lock`.
        self._web_off_explained = ""
        self._web_off_explained_said = True
        #: What the runtime document was built without when this process last
        #: compiled it (`runtime_config.LeftOut`); None before the first
        #: compile. Written by whichever lane compiled, under
        #: `_runtime_config_lock`; said on GTK (`_note_left_out`).
        self._compiled_left_out: tuple[runtime_config.LeftOut, ...] | None = None
        #: Why ``settings.toml`` can't be changed when the compiled document
        #: left its default out, or empty; kept with `_compiled_left_out`.
        self._compiled_default_pointer = ""
        #: The set that was last said, so it is said again only when it changes.
        self._left_out_said: frozenset[runtime_config.LeftOut] = frozenset()
        #: Whether the last status showed a service that reads presets.
        self._presets_readable_seen = False
        #: And one that reads the renderer dials (schema 7).
        self._dials_readable_seen = False
        # Whether the last status listed schema 8 (`_publish_again_when_presets_become_readable`).
        self._nothing_readable_seen = False
        #: A republish was already asked for what is left out (`_publish_again_...`).
        self._left_out_retried = False
        #: The service process the last status came from (its ``runtime_instance``).
        self._runtime_instance_seen: str | None = None
        self._default_pointer_said = ""
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
        #: The publication of web wallpapers going off is owed from this authoring
        #: generation on (`_publish_web_off`), 0 when none is: until one at or past it
        #: lands, no scan, barrier or library freshness holds a publication, and one
        #: that is held anyway runs the web-off guard (`_stopping_web_after_failure`).
        self._runtime_web_off_owed = 0
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
        # The runtime.toml texts replaced since the service last confirmed a
        # reload: what it may still hold if it refused one since (at most
        # HELD_CANDIDATES; past that, what it holds isn't known, but whether
        # one that was dropped allowed web wallpapers is). Each is numbered in
        # the order it was replaced, so that a reload confirms only the texts
        # replaced before it was sent (`_service_holds_what_is_on_disk`).
        self._held_candidates: list[tuple[int, str]] = []
        self._held_replaced = 0
        self._held_candidates_unknown = False
        self._held_dropped_through = 0
        self._held_dropped_allowing = False
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
        #: Settings → Tidy up and its card: one ordered lane, read-only until a click.
        self.tidy_lane = TidyLane()
        #: Settings → Setup and the window's setup card look on this lane; they
        #: only read (`wall_in_one.ui.setup_section`).
        self.setup_lane = SetupLane()
        #: The setup findings the card's Not Now hid, by fingerprint: for this
        #: process only, and never written (docs/missing-components.md).
        self.setup_dismissed: set[str] = set()
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
        #: `refresh_library(finished=...)` callbacks, told whether the next
        #: library install landed (True) or the refresh failed (False).
        self._library_refresh_waiters: list[Callable[[bool], None]] = []
        self._stills = StillMaker(
            report=self.window_report,
            note=lambda message: self.activity_log.record("app", message),
            web_allowed=lambda: self._settings.allow_web_wallpapers,
            previews=lambda: self._settings.preview_stills,
        )

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
        # A store saved outside the authoring lane (health, scans, settings
        # rows) whose folder sync failed is said in the window too.
        self._previous_durability_sink = state_file.set_durability_sink(self._not_durable)
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
        self._authoring_gate_reason = ""
        self._authoring_repair_started = True

        deferred = self.authoring_off_thread(
            self._repair_dangling_playlist_references,
            self._finish_playlist_reference_repair,
            requires_migration=False,
            queue_if_busy=True,
        )

        def replied(response: Response) -> None:
            if response.ok or self._authoring_shutdown:
                return
            if (
                self._authoring_gate_state == "checking"
                and self._authoring_repair_started
                and not self._authoring_migration_ready
            ):
                # The repair never ran to its end: its lane's guard refused it
                # (the profile lock held past its wait, or not lockable at
                # all), or its work raised. Nothing else moves the gate, so
                # left at ``checking`` it would say "still opening; retry
                # shortly" for good. It rests, with the reason, where Try
                # again (or a restart) runs the repair once more. First, and
                # the report last: saying it reaches the window.
                self._authoring_repair_started = False
                self._authoring_gate_state = "repair"
                self._authoring_gate_reason = response.message
                self._first_run_changed()
            self.window_report(response.message)

        deferred.start(replied)

    @staticmethod
    def _repair_dangling_playlist_references() -> _PlaylistReferenceRepairResult:
        """Remove only references whose playlist identity is durably absent.

        A reference is to a playlist's id, but an older profile's default,
        rule or display assignment may name its playlist by name, which the
        compiler resolves too (by id, then by name, ignoring case). The
        generated All media playlist is never authored but is always valid; its
        id is left alone and its exact name is pointed at that id. Such a
        reference is not dangling: it is pointed at the playlist's id, in the
        existing key or record, so it keeps working and a later rename can't
        leave it naming nothing. Only a reference that resolves to no
        playlist at all is removed.

        A store saved by a newer version is skipped rather than failed: it is
        read-only here, so it cannot be repaired, and pausing every write
        (and the first library scan) for it would show an empty app over
        readable files. A newer ``playlists.json`` skips the whole repair, because
        what this build can parse of it cannot prove which playlists are
        gone; repairing from it would delete the user's rules, display
        assignments and default. This build cannot delete playlists from that
        file either, so it creates no new dangling references, and runtime
        compilation refuses the faulted file meanwhile.
        """
        playlist_store = playlists.Store.open()
        if playlist_store.fault_kind == state_file.NEWER_VERSION:
            return _PlaylistReferenceRepairResult()
        if playlist_store.fault is not None:
            return _PlaylistReferenceRepairResult(failures=(f"playlists: {playlist_store.fault}",))
        valid = frozenset(playlist.id for playlist in playlist_store.all())
        failures: list[str] = []
        repaired_schedules: list[str] = []
        repaired_displays: list[str] = []
        retargeted_schedules: list[tuple[str, str]] = []
        retargeted_displays: list[tuple[str, str]] = []

        def named(reference: str) -> str | None:
            """The id of the playlist ``reference`` names by name, as the compiler finds it.

            The generated playlist's name is pointed at its id like an authored
            name, so the window, which keys playlists by id, finds it.
            """
            if reference == runtime_config.FALLBACK_PLAYLIST_NAME:
                return runtime_config.FALLBACK_PLAYLIST_ID
            found = playlist_store.by_name(reference)
            return found.id if found is not None else None

        schedule_store = schedules.Store.open()
        if schedule_store.fault_kind == state_file.NEWER_VERSION:
            pass
        elif schedule_store.fault is not None:
            failures.append(f"schedule rules: {schedule_store.fault}")
        else:
            dangling = sorted(
                reference
                for reference in {rule.playlist for rule in schedule_store.rules} - valid
                if reference != runtime_config.FALLBACK_PLAYLIST_ID
            )
            for playlist_id in dangling:
                target = named(playlist_id)
                try:
                    if target is not None:
                        if schedule_store.retarget(playlist_id, target):
                            retargeted_schedules.append((playlist_id, target))
                        continue
                    schedule_changed = schedule_store.forget_playlist(playlist_id)
                except schedules.ScheduleError as error:
                    failures.append(f"schedule rules for {playlist_id}: {error}")
                else:
                    if schedule_changed:
                        repaired_schedules.append(playlist_id)

        display_store = displays.Store.open()
        if display_store.fault_kind == state_file.NEWER_VERSION:
            pass
        elif display_store.fault is not None:
            failures.append(f"display assignments: {display_store.fault}")
        else:
            dangling = sorted(
                reference
                for reference in {playlist for _connector, playlist in display_store.all()} - valid
                if reference != runtime_config.FALLBACK_PLAYLIST_ID
            )
            for playlist_id in dangling:
                target = named(playlist_id)
                try:
                    if target is not None:
                        if display_store.retarget(playlist_id, target):
                            retargeted_displays.append((playlist_id, target))
                        continue
                    display_count = display_store.forget_playlist(playlist_id)
                except displays.DisplayError as error:
                    failures.append(f"display assignments for {playlist_id}: {error}")
                else:
                    if display_count:
                        repaired_displays.append(playlist_id)

        saved_settings: config.Settings | None = None
        try:
            loaded = config.load_strict_document()
            active = loaded.settings.active_playlist
            if active and active not in valid and active != runtime_config.FALLBACK_PLAYLIST_ID:
                target = named(active)
                try:
                    saved_settings = _settings_published(
                        lambda: (
                            config.forget_playlist_default(active)
                            if target is None
                            else config.repoint_playlist_default(active, target)
                        )
                    )
                except config.ConfigError as error:
                    # Only a settings.toml that can't be changed for as long
                    # as it stays as it is (keys this build doesn't know, or
                    # kept read-only) is not this start's to wait on. Nothing
                    # depends on the default being cleared: one that names
                    # nothing is left out by every compile, which says so
                    # (`runtime_config.LeftOut`), and one that still names
                    # its playlist, by name, has nothing wrong with it. A
                    # lock held too long or a file that stopped parsing is
                    # the failure it always was, below, so the person can
                    # try again and have the file put right.
                    if not config.why_settings_cannot_change(loaded, refused=error):
                        raise
                else:
                    if saved_settings is None:
                        durable_settings = config.load_strict()
                        if (
                            durable_settings.active_playlist
                            and durable_settings.active_playlist not in valid
                            and not runtime_config.is_fallback_reference(
                                durable_settings.active_playlist
                            )
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
            schedule_retargets=tuple(retargeted_schedules),
            display_retargets=tuple(retargeted_displays),
            failures=tuple(failures),
        )

    def _finish_playlist_reference_repair(
        self,
        result: _PlaylistReferenceRepairResult,
    ) -> Response:
        """Adopt committed repair deltas, opening the gate only when clean.

        The repair's writes are saved before this runs. If adopting them here
        fails, the gate rests at ``repair`` (Try again runs it once more and
        finds nothing left to repair), never at ``checking`` until a restart;
        the lane says what was saved (`SAVED_NOT_APPLIED`). What that repair
        saved is adopted again by the next one, before its own: on disk it is
        done, so Try again would otherwise leave memory naming what it removed.
        """
        earlier = self._unadopted_repairs
        try:
            for unadopted in earlier:
                self._adopt_playlist_reference_repair(unadopted, again=True)
            self._adopt_playlist_reference_repair(result)
        except Exception:
            self._unadopted_repairs = (*earlier, result)
            self._authoring_repair_started = False
            self._authoring_gate_state = "repair"
            self._first_run_changed()
            raise
        self._unadopted_repairs = ()
        return self._open_after_playlist_reference_repair(result)

    def _adopt_playlist_reference_repair(
        self, result: _PlaylistReferenceRepairResult, *, again: bool = False
    ) -> None:
        """The repair's deltas, in this process's stores and settings.

        Each is a semantic delta, so adopting one ``again`` changes nothing it
        already changed. ``again``, only the default it set is taken from its
        settings: nothing newer may be replaced by that older snapshot.
        """
        self._session.schedules.adopt_worker_forget_playlists(result.schedule_playlists)
        self._session.displays.adopt_worker_forget_playlists(result.display_playlists)
        self._session.schedules.adopt_worker_retarget(dict(result.schedule_retargets))
        self._session.displays.adopt_worker_retarget(dict(result.display_retargets))
        if result.settings is not None:
            previous = self._settings
            settings = (
                replace(previous, active_playlist=result.settings.active_playlist)
                if again
                else result.settings
            )
            with self._settings_authoring_lock:
                self._settings_requested = settings
            self._settings = settings
            self._session.update_settings(settings, rescan_library=False)
            if self._window is not None:
                self._window.apply_settings(settings)
            if previous.active_playlist != settings.active_playlist:
                self._session.playlists_changed()
        elif result.schedule_playlists or result.schedule_retargets:
            self._session.playlists_changed()

    def _open_after_playlist_reference_repair(
        self, result: _PlaylistReferenceRepairResult
    ) -> Response:
        """Open the gate after a clean repair, or rest it at ``repair``."""
        if result.failures:
            self._authoring_repair_started = False
            self._authoring_gate_state = "repair"
            self._first_run_changed()
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
        self._first_run_changed()
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
            # Ends the progress spinner; plain idle (200) starves below GTK's redraw (120).
            lambda done: GLib.idle_add(  # type: ignore[call-arg]
                self._deliver_legacy_migration_job, request, done, priority=GLib.PRIORITY_DEFAULT
            )
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
        """Present the window, building it first when there is none.

        Every activation lands here: the first launch, a second launch that
        Gio forwards, the desktop, and `present_page` (``ctl open`` and the
        in-app links). Presenting an existing window is all a repeat
        activation does. Only a new window does more: it is shown what the
        application already has, it asks the migration question while that
        is still open, and the process's first window resolves the palette
        and loads the library. A window rebuilt after the last one closed
        rescans the library once instead.
        """
        created = self._window is None
        if created:
            window = self._build_window()
            # Connected on the concrete GTK class: the signal is not part of
            # what the application asks of a window once it is stored.
            window.connect("close-request", self._on_close_request)
            self._window = window
            self._window_generation += 1
            # A new window starts from nothing; replay what the previous one
            # was showing. This is per window, not a model publication.
            if self._runtime_action_pending:
                window.set_runtime_busy(True)
            if self._status_model.status is not None:
                window.show_runtime_status(self._status_model.status)
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
        if not created:
            # The palette monitor and the post-hook keep the colours current,
            # and F5 rescans; re-resolving or rescanning here made every
            # `ctl open` cost a shell round trip and a library walk.
            return
        self._replay_application_state(self._window)
        if self._first_activation_done:
            # Rebuilt after the last window closed. Nothing watches the library
            # folders, so files added meanwhile need one scan; it runs off GTK
            # and replaces the held library shown above. The palette monitor
            # kept the colours current and the migration gate is still open.
            if self._settings.roots:
                self.refresh_library()
            return
        # Once authoring is open the predecessor has its disposition, and
        # the gate never closes again: a new window has nothing to ask.
        if not self._authoring_migration_ready and self._prompt_for_legacy_migration():
            return
        self._continue_first_activation()

    def _replay_application_state(self, window: WindowServices) -> None:
        """Show a newly built window the palette and library the process holds.

        The palette is application state that its monitor and the post-hook
        keep current without a window, so a new window is only shown it. The
        held library is shown at once, so a rebuilt window is not empty while
        `do_activate`'s fresh scan runs.
        """
        if self._resolved is not None:
            window.show_palette(self._resolved)
        # Before the first activation, or with no library folder chosen yet,
        # there is no library to show; the window keeps saying so.
        if not self._first_activation_done or not self._settings.roots:
            return
        window.show_library(self._session)

    def _build_window(self) -> MainWindow | NextWindow:
        """The window this process was started with: `NextWindow` unless it is classic."""
        if self._ui == "next":
            return NextWindow(self, self._settings)
        return MainWindow(self, self._settings)

    def _continue_first_activation(self) -> None:
        """Scan or ask for a root only after legacy authoring has a disposition.

        Once per process: the migration and repair completions call this too,
        and a window built later is shown the results instead (see
        `do_activate`).
        """
        if not self._authoring_migration_ready:
            self._activation_waiting_for_repair = True
            return
        if self._first_activation_done:
            return
        self._first_activation_done = True
        self._report_newer_version_files()
        self.reload_palette()
        if self._settings.roots:
            self.refresh_library()
        else:
            self._prompt_for_library_root()

    def _report_newer_version_files(self) -> None:
        """Say once, without blocking, which files only a newer version may change.

        The Session read every store when it opened, so this is an in-memory
        check. Each refused edit still explains itself; this is the warning
        before the first one.
        """
        if self._newer_version_notice_shown or self._window is None:
            return
        names = self._session.newer_version_files()
        if not names:
            return
        self._newer_version_notice_shown = True
        if len(names) == 1:
            self.window_report(
                f"{names[0]} was saved by a newer version of Wall-in-One. It is read-only "
                "here; open that version to change it."
            )
            return
        listed = ", ".join(names[:-1]) + f" and {names[-1]}"
        self.window_report(
            f"{listed} were saved by a newer version of Wall-in-One. They are read-only "
            "here; open that version to change them."
        )

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
        self._ask(dialog)

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
        # Closing the window must still close it. libadwaita answers a
        # window's close by closing its dialog instead, and this one can't
        # close: it only emits "close-attempt", and the window used to stay
        # (with the process) until the work ended, the close lost. Escape is
        # kept from the dialog, so an attempt is the window being closed.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda _keys, key, _code, _mods: key == Gdk.KEY_Escape)
        dialog.add_controller(keys)
        dialog.connect("close-attempt", self._close_window_past_progress)
        self._legacy_migration_prompt = dialog
        self._ask(dialog)

    def _close_window_past_progress(self, dialog: Adw.AlertDialog) -> None:
        """The window was asked to close over a progress dialog: put it away, then close.

        The work under the dialog does not need a window: a check, import or
        postponement that lands without one is consumed as a windowless one
        (`_deliver_legacy_migration_job`), and the next window asks again.
        The close is asked for again once this one has been answered, when
        no dialog is left to stop it.
        """
        window = self._dialog_parent()
        if self._legacy_migration_prompt is dialog:
            self._legacy_migration_prompt = None
        dialog.force_close()
        if window is None:
            return
        closing = window

        def close() -> bool:
            if closing.get_realized():
                closing.close()
            return GLib.SOURCE_REMOVE

        # After the close request that was turned away has returned.
        GLib.idle_add(close, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _close_legacy_migration_dialog(self) -> None:
        dialog = self._legacy_migration_prompt
        self._legacy_migration_prompt = None
        if dialog is not None:
            dialog.force_close()
            self._first_run_changed()

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
            # Ends the progress spinner; plain idle (200) starves below GTK's redraw (120).
            lambda done: GLib.idle_add(  # type: ignore[call-arg]
                self._deliver_legacy_migration_job, request, done, priority=GLib.PRIORITY_DEFAULT
            )
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

    def _deliver_legacy_migration_job(
        self,
        request: _LegacyMigrationRequest,
        future: Future[_LegacyMigrationResult],
    ) -> bool:
        """`_finish_legacy_migration_job`, then tell a window that asks in place."""
        try:
            return self._finish_legacy_migration_job(request, future)
        finally:
            self._first_run_changed()

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
        self._ask(dialog)

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
            self._first_run_changed()
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

    def _new_session(self, settings: config.Settings) -> Session:
        """The application's Session, which reports every library it scans (`_library_scanned`)."""
        session = Session(settings, owns_playback=self._service_start)
        session.on_scanned(self._library_scanned)
        return session

    def _library_scanned(self, sources: _LibrarySources) -> None:
        """A synchronous scan installed a library from ``sources``: they are accepted.

        `Session.refresh` says so only once the scan has succeeded and its
        library is installed, with the sources that scan read, whoever asked
        for it: the compatibility service's start and refreshes, the rescan
        when its settings change the library's sources, or a paused video's
        still. A failed scan says nothing, so the earlier evidence stands, and
        `_accepted_library_sources` stays None only for a library never
        scanned. (A GUI scans on a worker; `_adopt_runtime_library_scan` and a
        removal's own scan record those.)
        """
        with self._runtime_config_lock:
            self._accepted_library_sources = sources

    def _replace_session_after_migration(self, settings: config.Settings) -> None:
        """Reopen every split store which the just-completed import installed."""
        previous = self._session
        self._settings = settings
        self._settings_requested = settings
        self._session = self._new_session(settings)
        # Nothing the replaced one scans from now on is this application's.
        previous.on_scanned(None)
        with self._runtime_config_lock:
            # Its library is empty until it is scanned: nothing was accepted from it.
            self._accepted_library_sources = None
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
        self._ask(dialog)

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
        chooser.select_folder(self._dialog_parent(), None, self._on_initial_library_root_chosen)

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
        """Persist consent before the scan/download/still paths can use it.

        As an addition: on a first run nothing is saved yet, so this folder
        becomes the library; folders saved meanwhile by something else stay.
        """
        edit = config.RootsEdit.adding(root)
        self.edit_roots_async(
            edit,
            on_success=lambda _settings: self.window_report(
                edit.added_words(f"Using {root} as the wallpaper library")
            ),
            on_error=lambda error: self.window_report(
                f"Library folder was not saved; nothing changed: {error}"
            ),
        )

    # -- first run, as a window shows it ----------------------------------

    def _ask(self, dialog: Adw.AlertDialog) -> None:
        """Put one first-run question to the user.

        A window that asks in place (`FirstRunHost`, the new interface's
        welcome screen) is handed the dialog and answers through its
        ``response`` signal; any other window gets it presented, as always.
        """
        window: object = self._window
        if isinstance(window, FirstRunHost):
            window.ask(dialog)
            return
        dialog.present(self._dialog_parent())

    def _first_run_changed(self) -> None:
        """Tell a window that asks in place that a question or the gate moved."""
        window: object = self._window
        if isinstance(window, FirstRunHost):
            window.first_run_changed()

    @property
    def first_run_question(self) -> Adw.AlertDialog | None:
        """The first-run question open now (`_ask`): older data, or the library folder."""
        if self._legacy_migration_prompt is not None:
            return self._legacy_migration_prompt
        return self._library_root_prompt

    @property
    def first_run_pending(self) -> bool:
        """The first window's setup goes on without an answer: the older-data
        check, or the repair that follows it, is still running for this window.

        A welcome screen that is up stays up through these, rather than
        flashing the window between one question and the next.
        """
        if self._window is None or self._legacy_migration_deferred:
            return False
        return self._legacy_migration_future is not None or (
            self._activation_waiting_for_repair and self._authoring_repair_started
        )

    @property
    def first_activation_done(self) -> bool:
        """The process's first window has passed the gate and loaded (or asked for) a library."""
        return self._first_activation_done

    @property
    def authoring_gate(self) -> str:
        """``open``, or why changes can't be saved yet: ``checking``,
        ``decision`` (older data awaits a choice), ``probe-failed`` or ``repair``."""
        if self._authoring_migration_ready:
            return "open"
        return self._authoring_gate_state

    @property
    def authoring_gate_reason(self) -> str:
        """Why the gate rests at ``repair`` although no repair ran: the profile
        could not be opened (its lock, say). Empty for a repair that ran and
        left dangling references, and while the gate is anywhere else."""
        if self._authoring_migration_ready or self._authoring_gate_state != "repair":
            return ""
        return self._authoring_gate_reason

    def reconsider_older_data(self) -> bool:
        """Check for older data and ask about it again, now rather than next launch.

        For a window's notice while the decision is outstanding ("Not now", or
        a check that failed). It is the probe failure's own Try again: a fresh
        check off GTK, then the same question. False when there is nothing to
        reconsider now.
        """
        if (
            self._window is None
            or self._authoring_migration_ready
            or self._authoring_gate_state not in ("decision", "probe-failed")
            or self._legacy_migration_future is not None
            or self._legacy_migration_prompt is not None
        ):
            return False
        self._legacy_migration_prompted = False
        return self._prompt_for_legacy_migration()

    def retry_authoring_repair(self) -> bool:
        """Run the startup repair of dangling playlist references once more.

        The same transaction the gate runs before it opens; False unless that
        repair failed and nothing is running.
        """
        if (
            self._authoring_migration_ready
            or self._authoring_gate_state != "repair"
            or self._authoring_repair_started
        ):
            return False
        self._open_authoring_after_migration()
        return True

    def ask_for_library_folder(self) -> bool:
        """Ask for the first library folder again, after "Not now" in this process.

        The question is the first run's own: the exact detected folder as a
        suggestion, or a chooser. False when a folder is set, or the question
        can't be asked yet.
        """
        if (
            self._window is None
            or self._settings.roots
            or not self._first_activation_done
            or self._library_root_prompt is not None
        ):
            return False
        self._library_root_prompted = False
        self._prompt_for_library_root()
        return self._library_root_prompt is not None

    def _not_durable(self, message: str) -> None:
        """`state_file.report_not_durable`'s sink: any thread; said on GTK."""

        def say() -> bool:
            self.window_report(message)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(say, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def do_shutdown(self) -> None:
        # A shutdown that `request_quit` didn't ask for (a bare ``quit()``)
        # ends the loop with the window open and numbers still waiting out
        # their settle time. Hand them on before anything is torn down, and run
        # the loop until their writes settle, as a close or ``ctl quit`` lets
        # them (review r13b, G1): only theirs, and within its bound (r13c, G1).
        # Not a GTK teardown signal (`notify::root`): that runs while GTK is
        # already taking the window apart.
        self._shutting_down = True
        # A first Ctrl-C's countdown ends with the loop, the terminal back as it was.
        self._end_quit_countdown()
        if self._quit_without_waiting:
            # The person quit without them (a second Ctrl-C): dropped, never handed on.
            dial_rows.cancel_all()
        else:
            # A quit from elsewhere during the countdown hands on what it held, as any quit.
            self._let_held_authoring_go()
            self._hand_on_waiting_numbers()
        if self._numbers_unsettled():
            self._settle_before_shutdown()
        current = state_file.set_durability_sink(self._previous_durability_sink)
        if current != self._not_durable:
            state_file.set_durability_sink(current)  # another application's since: keep it
        # A deliberate quit (`ctl quit` in --service mode) ends the loop with
        # the window still open, and GTK then neither unrealizes nor destroys
        # it, so the teardown it runs on unrealize would not happen. Destroy it
        # first: the workers it owns stop with the application. A window that
        # was closed is already gone from here.
        # Typed as WindowServices, which says nothing about GTK.
        window: object = self._window
        self._window = None
        if isinstance(window, Gtk.Window):
            window.destroy()
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
            self._runtime_lane_at_exit = self._runtime_jobs
            self._runtime_jobs = None
        # What a publication the shutdown cut would have replaced, while web
        # wallpapers are off, is replaced on disk now, file-only (r13d, N11).
        self._web_off_on_disk_at_exit()
        self._stills.shutdown()
        self._session.shutdown()
        with self._browse_lock:
            self._browse_shutdown = True
            browse_transports = tuple(self._browse_transports)
            self._browse_transports.clear()
        for browser in browse_transports:
            browser.shutdown()
        self.tidy_lane.shutdown()
        self.setup_lane.shutdown()  # never waits: a look that hangs must not hold quit
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

    def _web_off_on_disk_at_exit(self) -> None:
        """Last in `do_shutdown`: a file that allows web wallpapers while they are off is replaced.

        A move of ``allow_web_wallpapers`` saved while the shutdown settles is
        published on the runtime lane, ``--service`` included, and the
        shutdown can cut that publication (the settle's bound, a second
        Ctrl-C) before it installs anything; the web-off guard after it is
        skipped then (`_stopping_web_after_failure`), and nothing publishes
        after it. A save still being written when the loop ended gets no
        publication at all. Either way the service would go on playing a web
        page while the setting is off, until it next starts (review r13d,
        N11).

        So, when ``settings.toml`` says web wallpapers are off and the file on
        disk allows them (``allow_web_wallpapers = true``, compiled while they
        were on, which lets a web page through the service's type check), the file
        is replaced with the document that plays nothing
        (`runtime_config.nothing_to_play_document`),
        as `_web_off_not_running` replaces it when no service runs. Both are
        schema 8, so whatever could load the one loads the other, and the
        service's own poll of the file loads it. Nothing plays then until a
        publication gets through: the app's next start, or the service's,
        whose start check compiles again. The library isn't compiled here
        (review r13e, L1, L2): a compile that can't put an older document over
        a newer one reads every authoring store again from disk, under the
        migration guard, as the runtime lane does, and that takes a fifth of
        a second or more for a large library, with nothing to end it at a
        second Ctrl-C. The one this step made from the Session in memory put
        an older compile over another writer's newer document.

        A file that doesn't allow web wallpapers lets no page play on a
        service that reads schema 8: it reads each item's type before it
        starts the engine. One older than that (the status last heard lists
        no 8) has no such check, and plays a web page such a file names: so
        for it, the file is replaced as well when it names a Workshop item
        that is, or may be, a web page (review r13f, L2;
        `_may_play_web_while_off`). Only the ``project.json`` of each id the
        file names is read, never a scan of the Workshop (r13e, L1), so the
        look is bounded by the file; it runs on a thread of its own, whose
        wait ends at a second Ctrl-C, or after `SHUTDOWN_WORKSHOP_LOOK_SECONDS`,
        though one of its calls is slow (review r13g, S-L1), and for such a
        service an id it can't check, or hasn't reached, counts as a web page.
        With no status heard at all, only a web page the files confirm counts: such a
        service isn't running, and its start compiles again (or refuses with
        exit 78 a kept file that plays a web page while they are off), or it
        is current and refuses a page at launch itself, so an item that can't
        be checked (unsubscribed, or on a library that isn't mounted) leaves
        the file as it is. That replacement can't stop a service
        older than schema 8 that is still running after an update, though:
        it can't load the document that plays nothing, and keeps playing what
        it holds. So when such a service was heard and the file named a web
        page the files confirm, it is told to quit (`_quit_older_service`):
        the one socket call made here, bounded by
        `SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS`, ended by a second Ctrl-C, and
        never begun after one. Its next start then runs the updated start
        check, which compiles again, or refuses with exit 78 a kept file that
        plays a web page while they are off. For an item that can't be
        checked it isn't told: the log says to restart it. No compile for its
        schema is made here (r13e, L1 and L2). What the replacement does is see
        that nothing started next plays the page: an older service can't load
        it, and the updated one plays nothing until a publication gets
        through.

        Judged and installed in one hold of the compiler lock, on what is on
        disk then: ``settings.toml`` and the file are read again under it, so
        a document another writer installed before the lock was had is judged
        as it is (one compiled for web wallpapers on, once the setting is on
        again, is left), and nothing installed after it is touched (L2). The
        migration guard isn't taken: no authoring store is read, and the
        legacy import never writes this file. Nothing else is written, and
        nothing is asked of the service, but for the bounded ``quit`` above:
        the shutdown has cut the socket calls off, and one would hold the exit.

        Bounded, and waiting only for what can matter. A settings save that
        turns web wallpapers off and is still being written is waited for, for
        as long as such a save itself can take (`SETTINGS_SAVE_SECONDS`, its
        locks and its check of the service); with none, nothing is waited
        for. Then the setting and the file are looked at without the lock
        (every writer replaces each by a rename), and with nothing to replace,
        the lock isn't waited for either. Otherwise it is waited for briefly
        (`SHUTDOWN_WEB_OFF_LOCK_SECONDS`). Looking at the file costs a search
        of its text for the compiler's own line for the key, and for any
        spelling of the key (`_may_allow_web_wallpapers`), which a compiled
        document never has; only a hand edit that has one is parsed, on a
        thread whose wait a second Ctrl-C or the look's bound ends (review
        r13f, N2). So a quit takes at most the settle's bound, that save's
        bound while one is being written, the lock's, the look's
        (`SHUTDOWN_WORKSHOP_LOOK_SECONDS`, for the texts judged before the lock
        and under it together: a hand edit's parses, and, unless the service
        last heard reads schema 8, the look at the Workshop items), and, unless
        it reads 8, the ``quit``'s.

        What those waits leave is judged once it has settled, before the
        process exits (`_web_off_judged_once_landed`; review r13f, L1): a
        turn-off still being written when the wait for it ended (its bound,
        or a second Ctrl-C), and a file to replace whose lock wasn't had. A
        publication holding the lock reads the setting again under the
        setting's own lock before it installs (review r13c, D2), but one that
        read it before such a save landed rightly installs a document that
        allows web wallpapers. This process's own lane is one: a save that also
        changes the library's sources waits for `_runtime_removal_lock`, which
        the lane holds for its whole publication, and one under the migration
        marker's lock waits for the lane's hold of that; so the lane installed
        for web wallpapers on after the step, and the save then landed with
        nothing left to publish it. That judgement, on a thread of its own,
        waits for the save, then for this process's runtime lane (the
        process's exit waits for both anyway), then for the compiler lock
        (`SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS`; a holder then is another
        process's, which reads the landed setting), and judges what is on
        disk then, as here, settings and file read again under the lock. A
        save that failed left the setting on, so the file is left, and one
        that landed and was turned on again since is left as well (N3). Only
        a third Ctrl-C (which ends the process at its exit's join), or
        another process's writer holding the lock past that wait, gets past
        it; the next start's compile, or its check of a kept file, then
        judges the file.

        A second Ctrl-C, during the settle or here, ends the step's waits at
        once (review r13c, N-2): the save still being written is then not
        waited for, and the lock is only tried. Its handler is installed for
        the whole step (`_second_interrupt_ends_the_settle`), and each wait
        looks for it every `SHUTDOWN_INTERRUPT_POLL_SECONDS`, the lock's
        included (`runtime_config.compiler_lock`'s ``give_up``; review r13e,
        L1). What has already landed is acted on as ever, which waits for
        nothing. Never raises.
        """
        restore = self._second_interrupt_ends_the_settle()
        try:
            self._replace_web_off_on_disk()
        except Exception:
            LOGGER.exception(
                "the runtime configuration on disk could not be checked against "
                "Allow web wallpapers at shutdown"
            )
        finally:
            restore()

    def _replace_web_off_on_disk(self) -> None:
        """`_web_off_on_disk_at_exit`'s work."""
        saving = self._turn_off_in_flight()
        target = paths.runtime_config_path()
        source = paths.settings_path()
        look = _ExitLook(self.runtime_status)
        settled = True
        cut = lambda: self._settle_cut  # noqa: E731
        if self._web_off_but_allowed_on_disk(target, source, look, cut) is not None:
            settled = self._replaced_under_the_lock(
                target,
                source,
                look,
                timeout=0.0 if self._settle_cut else SHUTDOWN_WEB_OFF_LOCK_SECONDS,
                give_up=cut,
            )
        if saving is not None or not settled:
            self._web_off_judged_once_landed(saving, target, source, look.status)

    def _replaced_under_the_lock(
        self,
        target: Path,
        source: Path,
        look: _ExitLook,
        *,
        timeout: float,
        give_up: Callable[[], bool],
    ) -> bool:
        """In one hold of the compiler lock: judged again, and replaced if it must be.

        Whether the lock was had (in ``timeout``, unless ``give_up`` first):
        `_web_off_but_allowed_on_disk` on what is on disk now, and the document
        that plays nothing installed in this same hold when it says so. When
        the file named a web page the files confirm, for a service older than
        schema 8 that was heard, that service is then told to quit, with the
        lock let go (`_quit_older_service`).
        """
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(
                    runtime_config.compiler_lock(target, timeout=timeout, give_up=give_up)
                )
            except runtime_config.RuntimeConfigError:
                return False
            judged = self._web_off_but_allowed_on_disk(target, source, look, give_up)
            if judged is None:
                return True
            settings, why = judged
            older = look.status is not None and not runtime_config.nothing_to_play_supported(
                look.status
            )
            quit_older = why == _NAMES_WEB and older
            document = runtime_config.nothing_to_play_document(
                settings, runtime_config.WEB_OFF_REASON
            )
            replaced = True
            try:
                runtime_config.install_nothing_to_play(document, target)
            except runtime_config.RuntimeConfigNotDurableError as uncertain:
                LOGGER.warning("%s", uncertain)  # installed: only its folder sync failed
            except runtime_config.RuntimeConfigError as error:
                replaced = False
                LOGGER.warning(
                    "Allow web wallpapers is off, and the configuration on disk could let a web "
                    "wallpaper play, but it couldn\u2019t be replaced at shutdown: %s",
                    error,
                )
            if not replaced:
                _log_restart_advice(why, look, older=older, quit_older=quit_older)
            elif why == _ALLOWS:
                LOGGER.info(
                    "Allow web wallpapers is off, but the configuration on disk may have "
                    "allowed web wallpapers; it was replaced at shutdown with one that plays "
                    "nothing"
                )
                if older:
                    LOGGER.warning(
                        "That service is older than this app, so it keeps playing what it "
                        "holds until it is restarted: systemctl --user restart wall-in-one"
                    )
            elif not older:
                LOGGER.warning(
                    "Allow web wallpapers is off, and the configuration on disk named a web "
                    "wallpaper, with no wallpaper service heard from; it was replaced at "
                    "shutdown with one that plays nothing"
                )
            else:
                LOGGER.warning(
                    "Allow web wallpapers is off, and the configuration on disk named %s, for a "
                    "wallpaper service older than this app; it was replaced at shutdown with "
                    "one that plays nothing, which that service can\u2019t load%s",
                    "a web wallpaper"
                    if quit_older
                    else "a Workshop item whose type couldn\u2019t be checked",
                    ": it is told to quit"
                    if quit_older
                    else ", so it keeps playing what it holds until it is restarted: "
                    "systemctl --user restart wall-in-one",
                )
        if quit_older:
            self._quit_older_service()
        return True

    def _quit_older_service(self) -> None:
        """Tell the wallpaper service to quit: at most `SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS`.

        Only from `_replaced_under_the_lock`, and only when a status from a
        service older than schema 8 was heard, web wallpapers are off, and the
        file on disk named a web page the files confirm (review r13f, L2's
        residual; the owner's call). Such a service can't load the document
        that plays nothing, and plays the page it holds until it stops.
        ``quit`` ends it cleanly, so systemd leaves it stopped, and whatever
        starts it next runs the updated start check
        (``--service-startup-prepare``: a compile, or exit 78 for a kept file
        that plays a web page while they are off).

        The one socket call the end of a shutdown makes, and bounded: its own
        timeout is that bound, it runs on a daemon thread (`_unless_given_up`)
        whose wait ends at the bound or at a second Ctrl-C, never later, and
        its socket is then closed (a `client.Cancellation` of its own). So it
        holds neither ``run()`` nor the process's exit past the bound. Not sent
        at all once a second Ctrl-C has come. What was sent, and what came of
        it, is logged. Never raises.
        """
        if self._settle_cut:
            LOGGER.warning(
                "the wallpaper service, older than this app, wasn\u2019t told to quit at "
                "shutdown: another Ctrl-C had come; restart it: systemctl --user restart "
                "wall-in-one"
            )
            return
        cancellation = client.Cancellation()
        bound = SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS
        restart = "restart it: systemctl --user restart wall-in-one"

        def send() -> str:
            try:
                reply = client.send_runtime("quit", timeout=bound, cancellation=cancellation)
            except client.NotRunningError:
                return "it wasn\u2019t running"
            except (client.ControlError, OSError) as error:
                return f"it couldn\u2019t be told ({error}); {restart}"
            if reply.ok:
                return "it is quitting"
            return f"it refused ({reply.message or 'no reason given'}); {restart}"

        outcome = _unless_given_up(
            send, lambda: self._settle_cut, cut=None, until=time.monotonic() + bound
        )
        if outcome is None:
            cancellation.cancel()  # its socket closed: nothing is left waiting on it
            outcome = (
                f"another Ctrl-C ended the wait for its answer; {restart}"
                if self._settle_cut
                else f"it didn\u2019t answer within {bound:g} s; {restart}"
            )
        LOGGER.warning(
            "the wallpaper service, older than this app, was sent quit at shutdown, since it "
            "may be playing a web wallpaper while they are off: %s. Its next start runs the "
            "updated start check",
            outcome,
        )

    def _web_off_judged_once_landed(
        self, saving: Future[Any] | None, target: Path, source: Path, status: object
    ) -> None:
        """`_replaced_under_the_lock` once ``saving`` and this process's runtime lane are done.

        On a thread of its own (`_web_off_deferred`), which isn't a daemon: the
        process's exit waits for it, as it waits for the save's worker and the
        lane's (`_runtime_lane_at_exit`), so it takes no longer than they do
        but for its own wait for the compiler lock
        (`SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS`), its look at the file
        (`SHUTDOWN_WORKSHOP_LOOK_SECONDS`: a hand edit's parses, and the look
        at the Workshop items it names), and the ``quit``
        (`SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS`).
        ``target`` and ``source`` are the files as the step found them, and
        ``status`` what it had last heard. Only files and that lock are
        touched, nothing of GTK; the one socket call is `_quit_older_service`'s,
        when it is warranted.
        """
        lane = self._runtime_lane_at_exit
        never = lambda: False  # noqa: E731

        def judge() -> None:
            try:
                if saving is not None:
                    futures.wait((saving,))
                if lane is not None:
                    lane.shutdown(wait=True)
                if not self._replaced_under_the_lock(
                    target,
                    source,
                    _ExitLook(status),
                    timeout=SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS,
                    give_up=never,
                ):
                    look = _ExitLook(status)
                    read: list[bool] = []
                    judged = self._web_off_but_allowed_on_disk(
                        target, source, look, never, read=read
                    )
                    if judged is None and read:
                        LOGGER.warning(
                            "the runtime configuration on disk needs no replacement at exit "
                            "(Allow web wallpapers is on now, or the file on disk can\u2019t let a "
                            "web wallpaper play; it was not replaced under the lock: the compiler "
                            "lock couldn\u2019t be taken within %s s)",
                            SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS,
                        )
                    elif judged is None:
                        LOGGER.warning(
                            "the runtime configuration on disk was not checked against Allow web "
                            "wallpapers at exit: the compiler lock couldn\u2019t be taken within "
                            "%s s; the next start judges it",
                            SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS,
                        )
                    else:
                        LOGGER.warning(
                            "Allow web wallpapers is off, and the configuration on disk could "
                            "let a web wallpaper play, but it couldn\u2019t be replaced at exit: "
                            "the compiler lock couldn\u2019t be taken within %s s",
                            SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS,
                        )
                        # read only, unlocked: the file is left as it is
                        older = status is not None and not runtime_config.nothing_to_play_supported(
                            status
                        )
                        _log_restart_advice(judged[1], look, older=older, quit_older=False)
            except Exception:
                LOGGER.exception(
                    "the runtime configuration on disk could not be checked against "
                    "Allow web wallpapers at exit"
                )

        thread = threading.Thread(target=judge, name=WEB_OFF_AT_EXIT_THREAD)
        self._web_off_deferred = thread
        thread.start()

    def _web_off_but_allowed_on_disk(
        self,
        target: Path,
        source: Path,
        look: _ExitLook,
        give_up: Callable[[], bool],
        *,
        read: list[bool] | None = None,
    ) -> tuple[config.Settings, str] | None:
        """``source`` (``settings.toml``) as read now, and why ``target`` must be replaced: when
        the setting is off and a service may play a web page from the file (`_ExitLook.why`,
        whose look ``give_up`` ends: then it counts as one); otherwise None. Read now, never a
        snapshot of before: a turn-off still being written is judged once it has landed
        (`_web_off_judged_once_landed`), and a save turned on since is what it says (review
        r13f, N3). ``read``, when given, gets an item once the setting has been read as on, or
        once it has been read as off and the file has been read too (so a None after that means
        they need no replacing, not that they weren't looked at; the file was not read when the
        setting is on)."""
        try:
            settings = config.load_strict(source)
        except config.ConfigError:
            return None  # as for `_web_off_guard`: the next compile says what is wrong with it
        if settings.allow_web_wallpapers:
            if read is not None:
                read.append(True)
            return None
        try:
            on_disk = file_io.read_regular_text(target, runtime_config.MAX_RUNTIME_CONFIG_BYTES)
        except OSError, UnicodeDecodeError:
            return None
        if on_disk is None:
            return None
        if read is not None:
            read.append(True)
        why = look.why(on_disk, give_up)
        return (settings, why) if why else None

    def _turn_off_in_flight(self) -> Future[Any] | None:
        """Wait for a settings save still being written: until it ends, its bound, or a Ctrl-C.

        Only a save at the head of the authoring lane, unsettled: the
        worker's run is then that save's. A number's write, say, isn't waited
        for. When the shutdown handed Settings on behind it, the save is theirs,
        which lands after it (`_settings_handed_on`). A second Ctrl-C ends the
        wait (`_second_interrupt_ends_the_settle`,
        installed by the step for all of it); one during the settle means it
        isn't begun. Such a save, when it turns web wallpapers off
        (`_settings_batch_turns_web_off`) and still isn't done when the wait
        ends; otherwise None. It lands before the process exits all the same,
        and is judged then (`_web_off_judged_once_landed`). One that is done
        has said what it saved in ``settings.toml``, or failed.
        """
        with self._settings_authoring_lock:
            saving_settings = self._settings_batch_generation is not None
            turns_web_off = self._settings_batch_turns_web_off
        saving = self._authoring_future if saving_settings else None
        if saving is None:
            return None
        deadline = time.monotonic() + SETTINGS_SAVE_SECONDS
        while not saving.done() and not self._settle_cut:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # In slices: Python runs the Ctrl-C handler between them.
            futures.wait((saving,), timeout=min(remaining, SHUTDOWN_INTERRUPT_POLL_SECONDS))
        return saving if turns_web_off and not saving.done() else None

    def _on_close_request(self, window: Gtk.Window) -> bool:
        # Every settings edit is persisted by ``update_settings``.  Saving the
        # in-memory recovery defaults here used to overwrite a malformed file,
        # and even a valid future document lost unknown keys merely because an
        # older app was opened and closed without an edit.
        # A number nudged under 450 ms ago is handed on now, to the wallpaper it
        # was entered for, through the same queued write the timer would use;
        # the accepted write keeps the process resident until it is durable.
        # Not a `notify::root` handler: that runs during teardown.
        self._hand_on_waiting_numbers()
        # The stored reference is typed as WindowServices, which says nothing
        # about GTK, so compare identities as plain objects.
        closing: object = window
        if self._window is closing:
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
        # A number nudged under 450 ms ago (`ctl quit` with the window open) is
        # handed on first: its write is one of those tails (review r13b, G1).
        self._hand_on_waiting_numbers()
        self._quit_requested = True
        self._maybe_finish_requested_quit()

    def run(self, argv: list[str] | None = None) -> int:
        """`Gio.Application.run`, with this application's own Ctrl-C while it runs (`_on_sigint`).

        Installed only where PyGObject would install its own, which calls
        ``quit()`` at the first Ctrl-C and ignores every later one while
        ``run()`` is on the stack: on the main thread, over Python's default
        handler. PyGObject then installs none, and still wakes the loop on a
        signal, so the handler runs promptly. Python's default handler is put
        back as ``run()`` returns, so that a Ctrl-C after that ends the
        process's exit, as it did. A Ctrl-C that quit at once, or came once
        the shutdown had begun, ends ``run()`` with KeyboardInterrupt, as
        PyGObject's does; a first Ctrl-C's careful quit returns as a close or
        ``ctl quit`` does.
        """
        handler: Callable[[int, Any], None] | None = None
        if (
            threading.current_thread() is threading.main_thread()
            and signal.getsignal(signal.SIGINT) is signal.default_int_handler
        ):
            handler = self._on_sigint
            try:
                signal.signal(signal.SIGINT, handler)
            except ValueError, OSError:
                handler = None
        self._sigint_handler = handler
        try:
            status = super().run(argv)
        finally:
            self._end_quit_countdown()
            if handler is not None and signal.getsignal(signal.SIGINT) is handler:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signal.SIGINT, signal.default_int_handler)
            self._sigint_handler = None
        if self._ctrl_c_quit:
            raise KeyboardInterrupt
        return status

    def _on_sigint(self, _number: int, _frame: Any) -> None:
        """SIGINT while ``run()`` runs: a Ctrl-C, handled on GTK's loop (`_ctrl_c`).

        Python runs it between any two bytecodes of GTK's thread, with a lock
        held as like as not, so it only asks the loop. Once the shutdown has
        begun the loop runs only in the settle's own iterations: a Ctrl-C then
        ends the shutdown's waits at once, as the handler they install for
        themselves does (`_second_interrupt_ends_the_settle`).
        """
        if self._shutting_down:
            self._settle_cut = True
            self._ctrl_c_quit = True
            return
        GLib.idle_add(self._ctrl_c, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _ctrl_c(self) -> bool:
        """One Ctrl-C, on GTK: the first quits carefully, any later one at once.

        The first is `_first_ctrl_c`. Once it counts down, or once it has said
        that a careful quit is saving the person's changes (the one its
        countdown ends in, or ``ctl quit``'s), a Ctrl-C quits at once, without
        what is still waiting to be saved (`_quit_without_waiting_changes`).
        Once the shutdown has begun, one ends its waits (`_on_sigint`), and
        once ``run()`` has returned, Python's own handler ends the process's
        exit: someone who keeps pressing it is out at the fourth at the most.
        A key during the countdown cancels the quit, and leaves nothing of it:
        the next Ctrl-C is a first again.
        """
        if self._shutting_down:
            self._settle_cut = True
            self._ctrl_c_quit = True
        elif self._quit_countdown is not None or self._ctrl_c_saving or self._quit_without_waiting:
            self._quit_without_waiting_changes()
        else:
            self._first_ctrl_c()
        return GLib.SOURCE_REMOVE

    def _first_ctrl_c(self) -> None:
        """A first Ctrl-C: a countdown when the person's changes are waiting, then a careful quit.

        With none of the person's changes waiting, at once and saying nothing,
        as before the countdown: ``quit()``, which hands on the renderer
        numbers and the Settings waiting (`do_shutdown`) and never waits for
        the application's own tails, a scan or a publication. With some
        waiting and a careful quit already under way that this Ctrl-C didn't
        start (``ctl quit`` in ``--service``), that quit goes on saving them,
        which can't be cancelled from here: it says so once, with no countdown
        and no key to cancel (`quit_countdown.saving`), and the next Ctrl-C quits without
        them (the owner's "save first"). Otherwise what is
        waiting is held for `QUIT_COUNTDOWN_SECONDS`, counted down on standard
        error (`quit_countdown`), so that the person can quit without it (a
        second Ctrl-C) or not quit at all (any other key), and then saved and waited for
        by `request_quit`. Held: the authoring lane starts nothing meanwhile,
        and admits only what a requested quit admits (`_admitted_during_quit`):
        a number whose timer fires, the Settings coalesced behind a batch that
        lands. A write already being written when it is pressed goes on.
        """
        changes = self._changes_a_quit_would_drop()
        if not changes:
            self._ctrl_c_quit = True
            self.quit()
            return
        if self._quit_requested:
            self._ctrl_c_saving = True
            quit_countdown.say(quit_countdown.saving(changes))
            return
        self._authoring_held = True
        self._quit_countdown = quit_countdown.Countdown(
            changes,
            QUIT_COUNTDOWN_SECONDS,
            QUIT_COUNTDOWN_TICK_MS,
            done=self._quit_countdown_done,
            cancelled=self._quit_countdown_cancelled,
        )
        try:
            self._quit_countdown.start()
        except Exception:
            LOGGER.exception("the countdown to quitting could not start; quitting as asked")
            self._end_quit_countdown()
            self._quit_countdown_done()

    def _changes_a_quit_would_drop(self) -> int:
        """How many of the person's changes aren't being written yet: what a second Ctrl-C drops.

        Each renderer number waiting out its settle time (`dial_rows.waiting`:
        only a row's own nudge starts one); each authoring task queued that is
        the person's (`_AuthoringTask.edit`: a gesture in either window, a
        number handed on included, whatever `authoring_action_async` is given
        but for the library's reconciliation after a scan and the ingestion
        of the service's health); and each Settings field accepted that no
        batch being written has taken (every one comes from a gesture: a
        Settings row, a playlist made the default, the first folder chosen, a
        scheme), a Settings batch still queued counting as its fields. Not
        one that turns web wallpapers off, which is never dropped
        (`_drop_waiting_settings`); not what is being written now, which is
        never stopped; and none of the application's own work: those two
        tasks, the repair that opens authoring, a scan, a publication.
        """
        with self._settings_authoring_lock:
            took = self._settings_batch_generation
            settings = sum(
                1
                for key, (accepted, value) in self._settings_authoring_pending.items()
                if (took is None or accepted > took)
                and not (key == "allow_web_wallpapers" and not value)
            )
        queued = sum(
            1
            for task in self._authoring_queue
            if task.edit and task.prepare != self._prepare_settings_batch
        )
        return dial_rows.waiting() + queued + settings

    def _quit_countdown_done(self) -> None:
        """The countdown is over: what it held is saved, and the application quits once it is."""
        self._quit_countdown = None
        self._ctrl_c_saving = True
        self._let_held_authoring_go()
        self.request_quit()

    def _quit_countdown_cancelled(self) -> str | None:
        """A key during the countdown: no quit, and what it held is saved as if it never ran.

        Unless ``ctl quit`` asked to quit meanwhile (``--service``): that quit
        goes on, saving what was held as a careful quit does, and the key says so
        (`QUIT_GOES_ON`) instead of that the quit was cancelled.
        """
        self._quit_countdown = None
        self._let_held_authoring_go()
        if not self._quit_requested:
            return None
        self._maybe_finish_requested_quit()
        return QUIT_GOES_ON

    def _let_held_authoring_go(self) -> None:
        """Let the authoring lane start what it held (`_authoring_held`), if it holds any."""
        if self._authoring_held:
            self._authoring_held = False
            self._start_next_authoring_task()

    def _end_quit_countdown(self, said: str = "") -> None:
        """Stop the countdown, if one runs: its timer, its watch of the terminal and the
        terminal's mode, its line finished and then ``said``. What it held stays held."""
        countdown, self._quit_countdown = self._quit_countdown, None
        if countdown is not None:
            countdown.stop(said)

    def _quit_without_waiting_changes(self) -> None:
        """Quit at once, without the changes still waiting to be saved: a Ctrl-C after the first.

        ``quit()``, as a first Ctrl-C with nothing waiting makes, except that
        what the person accepted but isn't being written yet is dropped
        instead of handed on: the numbers waiting out their settle time, the
        authoring tasks queued and the Settings no batch being written has
        taken (`do_shutdown`, `_shutdown_authoring_jobs`). The person chose not
        to save them. Never a change that turns web wallpapers off: it is
        saved, and the end of the shutdown waits for it and judges the file
        on disk by it, as for any such save (`_drop_waiting_settings`). What is
        being written goes on, and the process's exit waits for it. A quit that
        comes any other way still hands everything on, as before. Said once,
        during the countdown or after it, or after the line a first Ctrl-C
        says while ``ctl quit`` saves them (`QUIT_WITHOUT_THEM`).
        """
        said = "" if self._quit_without_waiting else QUIT_WITHOUT_THEM
        self._quit_without_waiting = True
        self._authoring_held = True
        self._ctrl_c_quit = True
        if self._quit_countdown is not None:
            self._end_quit_countdown(said)
        elif said:
            quit_countdown.say(said)
        self.quit()

    def _hand_on_waiting_numbers(self) -> int:
        """Save every renderer number still waiting out its settle time; how many.

        `dial_rows.flush_all`, for the ways the application ends that run
        this process's own code: a window's close, ``ctl quit`` and a first
        Ctrl-C's countdown (`request_quit`), and a quit that comes another way,
        a first Ctrl-C with none of the person's changes waiting among them,
        through `do_shutdown`; not a Ctrl-C after the first, which drops them
        (`_quit_without_waiting_changes`).
        SIGTERM (it has a handler only while a first Ctrl-C's countdown holds
        the terminal, and only to put the terminal back) and a lost display
        (GTK ends the process itself) run none of them: a number nudged under
        `dial_rows.SETTLE_MS` before either is lost (review r13c, N-9). Each
        takes the queued write its timer would have taken, admitted even
        once a quit was asked (`dial_rows.handing_on`); that write holds the
        application until it is durable (`_maybe_finish_requested_quit`).
        """
        return dial_rows.flush_all()

    def _admitted_during_quit(self, allow_during_quit: bool) -> bool:
        """Whether a write may still be admitted while a requested quit waits for its tails.

        What its caller allows, and a renderer number being handed on, by a
        flush or by its own timer (`dial_rows.handing_on`): the person
        entered it before the quit could end the process, so it is saved,
        as a close or ``ctl quit`` with the number still waiting saves it,
        and the quit waits for it too (review r13c, N-3).
        """
        return allow_during_quit or dial_rows.handing_on()

    def _numbers_unsettled(self) -> bool:
        """Whether a renderer number handed on hasn't settled: its write, or its publication.

        Its write while it is queued or being saved (`_AuthoringTask.handed_on`);
        then, when it saved something, the publication it asked for
        (`_number_publication`) for as long as the runtime lane holds it. No
        other tail of the application: a Settings save, a removal, a scan
        (review r13c, G1-L2). What a ``ctl quit`` waits for is `_quit_waits`,
        as before. Not one queued once a Ctrl-C asked to quit without it
        (`_quit_without_waiting`): the lane holds it, and the shutdown drops it.
        """
        current = self._authoring_current
        if current is not None and current.handed_on:
            return True
        if not self._quit_without_waiting and any(task.handed_on for task in self._authoring_queue):
            return True
        return self._number_publication and self._runtime_publication_held

    def _settle_before_shutdown(self) -> None:
        """Run the loop until the numbers `do_shutdown` handed on have settled.

        For `do_shutdown` only, once the loop has ended without that wait:
        their writes, and the publication they asked for (`_numbers_unsettled`).
        At most `SHUTDOWN_SETTLE_SECONDS`, and truly: nothing in it blocks
        the loop, since every publication from `do_shutdown` on runs on the
        runtime lane, ``--service`` included (`_publish_runtime_for_context`),
        whose socket calls the shutdown then cuts off (review r13c, G1-L1).
        A Ctrl-C ends it at once (`_second_interrupt_ends_the_settle`,
        N-2). Either way shutdown goes on as before, and every number handed
        on is still saved: a write already running reaches its durability
        boundary, and one still queued is started on the same worker
        (`_shutdown_authoring_jobs`).
        """
        context = GLib.MainContext.default()
        deadline = time.monotonic() + SHUTDOWN_SETTLE_SECONDS
        # Wakes the loop with nothing else to do, so the deadline is looked at.
        tick = GLib.timeout_add(100, lambda: GLib.SOURCE_CONTINUE)
        restore = self._second_interrupt_ends_the_settle()
        try:
            while (
                self._numbers_unsettled() and not self._settle_cut and time.monotonic() < deadline
            ):
                context.iteration(True)
        finally:
            restore()
            GLib.source_remove(tick)

    def _second_interrupt_ends_the_settle(self) -> Callable[[], None]:
        """Let a Ctrl-C during the settle end it; what puts the previous handler back.

        The last step of the shutdown installs it too, for all of its work,
        and its waits look for it: for a settings save (`_turn_off_in_flight`)
        and for the compiler lock (`_web_off_on_disk_at_exit`).

        PyGObject's SIGINT fallback calls ``quit()`` once and ignores every
        later SIGINT while ``run()`` is on the stack, which it still is in
        `do_shutdown`, so a second Ctrl-C did nothing for as long as the
        settle took (review r13c, N-2). While it runs, a SIGINT sets
        `_settle_cut` and is then handed to the handler it replaced, this
        application's own (`run`) or PyGObject's, so that ``run()`` still
        ends as a Ctrl-C ends it. Python runs the handler
        between the loop's iterations: PyGObject wakes the loop on a signal,
        and so does the settle's tick. Off the main thread, or with a handler
        that isn't Python's (or one that ignores it), nothing changes.
        """
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous = signal.getsignal(signal.SIGINT)
        if previous is None or previous in (signal.SIG_IGN, signal.SIG_DFL):
            return lambda: None
        own = self._sigint_handler

        def interrupted(number: int, frame: Any) -> None:
            self._settle_cut = True
            if callable(previous) and (
                (own is not None and previous is own)
                or getattr(previous, "__module__", "") == "gi._ossighelper"
            ):
                previous(number, frame)

        try:
            signal.signal(signal.SIGINT, interrupted)
        except ValueError, OSError:
            return lambda: None

        def restore() -> None:
            with contextlib.suppress(ValueError, OSError, TypeError):
                if signal.getsignal(signal.SIGINT) is interrupted:
                    signal.signal(signal.SIGINT, previous)

        return restore

    @property
    def legacy_service(self) -> bool:
        """Whether this Python process intentionally owns compatibility timers."""
        return self._service_start

    @property
    def ui(self) -> UiKind:
        """Which window this process builds, from ``--ui``."""
        return self._ui

    @property
    def runtime_status(self) -> dict[str, object] | None:
        """Last valid atomic Rust status, retained across transient timeouts."""
        return self._status_model.status

    @property
    def status_model(self) -> RuntimeStatusModel:
        """The application's single holder of runtime status, for windows to observe."""
        return self._status_model

    def _forward_runtime_status(self, change: StatusChange, view: RuntimeStatusView) -> None:
        """Hand one model publication to the window as its call site used to.

        Publications happen exactly where the window used to be called, so
        this is the only place runtime status reaches the window. A window
        that renders from the model itself may implement these as no-ops.
        The session log hears every publication too, window or not.
        """
        self.activity_log.observe(change, view)
        window = self._window
        if window is None:
            return
        match change:
            case StatusChange.STATUS:
                assert view.status is not None
                window.show_runtime_status(view.status)
            case StatusChange.UNAVAILABLE:
                window.show_runtime_unavailable()
            case StatusChange.DELAYED:
                window.show_runtime_delayed()
            case StatusChange.INVALID:
                window.show_runtime_protocol_error(view.protocol_error)
            case StatusChange.BUSY:
                window.set_runtime_busy(view.busy)

    def present_page(self, page: str) -> bool:
        """Present the singleton window with one primary workflow page visible.

        False when the window has no such page yet (the new interface while
        it is being ported); the window is presented either way and says so.
        """
        # In service mode there is no window yet. Activating this same
        # GApplication constructs one locally; an ordinary second invocation
        # is forwarded here by Gio for the same reason. With a window already
        # open, activation only presents it, so this stays cheap for the
        # panel's `ctl open` links and the in-app ones.
        self.activate()
        if self._window is None:  # pragma: no cover - a broken GTK invariant
            raise RuntimeError("Wall-in-One could not create its window")
        return self._window.show_page(page)

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
                # A reply on the theme lane: the lane's own priority.
                self._deliver_theme_result(self._deliver_rejected_palette_callback, on_complete)
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
                self._deliver_theme_result(
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
                self._deliver_theme_result(self._finish_wallpaper_query, wallpaper_result)
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
            self._deliver_theme_result(self._finish_palette_resolution, result)

    @staticmethod
    def _deliver_theme_result(callback: Callable[..., bool], *data: object) -> None:
        """Hand one theme-lane result to GTK ahead of its next redraw."""
        # PyGObject's idle_add override accepts priority=; its stubs omit it.
        GLib.idle_add(callback, *data, priority=THEME_DELIVERY_PRIORITY)  # type: ignore[call-arg]

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
                    # A reply on the theme lane: the lane's own priority.
                    self._deliver_theme_result(self._deliver_rejected_theme_callback, on_complete)
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

    def set_window_glass(self, glass: css.Glass | None) -> None:
        """The new window's glass dials (``ui.toml``), rendered into the stylesheet.

        ``None`` (the classic window) keeps settings.toml's window opacity. Any
        other value replaces it, since the new window's look lives in ui.toml.
        """
        if glass == self._window_glass:
            return
        self._window_glass = glass
        if self._resolved is not None:
            self._apply_stylesheet(self._resolved)

    def _apply_stylesheet(self, resolved: source.ResolvedPalette) -> None:
        glass = self._window_glass
        opacity = self._settings.opacity if glass is None else 1.0
        stylesheet = css.render(resolved.palette, opacity=opacity, glass=glass)
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

    @property
    def resolved_palette(self) -> source.ResolvedPalette | None:
        return self._resolved

    def open_palette_browser(self) -> None:
        """Open the palette browser, from wherever asked."""
        if self._window is not None:
            self._window.open_palette_browser()

    def window_report(self, message: str) -> None:
        """Put a management-page result in the window's toast overlay (and the log)."""
        message = self.with_task_durability(message)
        self.activity_log.record("app", message)
        if self._window is not None:
            self._window.report(message)

    def with_task_durability(self, message: str) -> str:
        """``message``, made true of the authoring task it reports, and carrying what it saved.

        Said while an authoring task's result is adopted on GTK (by its
        caller's report or toast, `_finish_authoring_outcome`):

        * a task whose write landed but whose adoption failed is said as the
          lane says it ("Saved, but it couldn't be applied: ..."), in place
          of the caller's first message, whose failure wording isn't true;
        * a task whose work changed something and then failed (a damaged file
          moved aside, its replacement refused) is never "nothing changed";
        * the first message also says what the task saved but couldn't
          confirm durable, so one task is one message.

        The lane says these on their own only when the caller says nothing.
        """
        task = self._speaking_for
        if task is None:
            return message
        spoken = task.spoken
        if spoken.instead and not spoken.instead_said:
            spoken.instead_said = True
            message = spoken.instead
        elif spoken.partly:
            message = _NOTHING_CHANGED.sub("", message)
        if spoken.warnings and not spoken.said:
            spoken.said = True
            message = _with_durability(message, spoken.warnings)
        return message

    def _dialog_parent(self) -> Gtk.Window | None:
        """The window as a dialog parent, when it is a real GTK window.

        Real application windows always are. `WindowServices` cannot say so; the
        narrowing keeps that GTK detail out of the Protocol.
        """
        window: object = self._window
        return window if isinstance(window, Gtk.Window) else None

    # -- library ---------------------------------------------------------

    @property
    def session(self) -> Session:
        return self._session

    def refresh_library(self, *, finished: Callable[[bool], None] | None = None) -> None:
        """Rescan and reconcile without filesystem work on GTK's thread.

        The explicit headless service (``--service``) keeps the synchronous
        :class:`Session` contract. Every GUI process, with a window or after
        its last window closed, uses one generation-tagged worker that replays
        the removal journal, walks the library, and performs confirmed Workshop
        cleanup through detached Stores. The existing grid, search, scroll and
        focus remain in place. A newer request cooperatively cancels the older
        one and makes any already-running result ineligible to land.

        ``finished``, when given, runs once on GTK: with True after the next
        library install has been shown (`_finish_library_refresh`), or with
        False when this refresh fails or cannot start. The new interface's
        Store uses it to find a fresh download in the library before applying
        it. Nothing calls it when the application is shutting down.
        """
        # An idle refresh can already be queued when the final window and
        # workers shut down. Window absence is not permission to re-enter the
        # synchronous headless path on a closed Session.
        if self._runtime_shutdown or self._library_scan_shutdown:
            return
        if finished is not None:
            self._library_refresh_waiters.append(finished)
        if self._removal_active:
            self._refresh_after_removal = True
            return
        if self._service_start:
            # The explicit compatibility service scans synchronously. A GUI
            # whose last window closed keeps the asynchronous path below.
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
                    self._library_scan_failed(detail)
                    self._mark_removal_scan_terminal(runtime_generation, failed=True)
                self.window_report(f"Library scan failed: {detail}")
                self._settle_library_refresh_waiters(False)
                return
            # Its own sources are accepted already (`_library_scanned`), as
            # this scan read them: never what the settings say afterwards.
            self._validate_inventory()
            try:
                self._finish_library_refresh()
            finally:
                if runtime_pending:
                    self._finish_runtime_library_scan(runtime_generation)
            return

        try:
            request = self._session.prepare_library_refresh()
        except LibraryRefreshBusyError as error:
            self.window_report(f"Library refresh will wait: {error}")
            # Only this request failed: a scan already running may still land.
            if finished is not None and finished in self._library_refresh_waiters:
                self._library_refresh_waiters.remove(finished)
                self._tell_library_refresh_waiter(finished, False)
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
        if self._window is not None:
            self._window.show_library_scanning(True)
        self._begin_runtime_library_scan(generation)

        try:
            future = self._library_scan_pool().submit(request.scan)
        except Exception as error:
            detail = str(error) or error.__class__.__name__
            request.cancel()
            if self._window is not None:
                self._window.show_library_scanning(False)
            self.window_report(f"Library scan could not start: {detail}")
            self._finish_runtime_library_scan(generation, error=detail)
            self._settle_library_refresh_waiters(False)
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
        # Shutting down: nobody is left to tell (see `refresh_library`).
        self._library_refresh_waiters.clear()
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
                self._settle_library_refresh_waiters(False)
                return GLib.SOURCE_REMOVE
            except Exception as error:  # defensive boundary around injected scanners too
                detail = str(error) or error.__class__.__name__
                if self._window is not None:
                    self._window.show_library_scanning(False)
                    self._window.report(f"Library scan failed: {detail}")
                self._finish_runtime_library_scan(generation, error=detail)
                self._settle_library_refresh_waiters(False)
                return GLib.SOURCE_REMOVE
            self._queue_library_reconciliation(generation, request, library)
            return GLib.SOURCE_REMOVE

        # It ends the scanning spinner and releases playback gestures waiting
        # on this scan: above redraw.
        GLib.idle_add(deliver, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

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
                # only semantic deletion facts, never the stale Library, and
                # say once what this cleanup couldn't finish: nobody else will.
                self._session.adopt_library_cleanups(result.cleanups)
                self._session.adopt_library_fault_repairs(result.repaired_faults)
                unfinished = _unfinished_library_cleanup(result)
                if unfinished:
                    self.window_report(unfinished)
                return
            try:
                self._session.adopt_library_refresh(result)
                # A scan landing as current started after any removal's commit
                # (`_invalidate_library_scan_after_removal`): an inventory.
                self._validate_inventory()
                # A later scan which actually sees the item again is the clean
                # reinstall boundary and may release its process-local taboo.
                self._removed_item_paths.difference_update(
                    item.path for item in result.library.items
                )
                self._adopt_runtime_library_scan(generation)
                if result.replay_failures:
                    self.window_report(_unfinished_replay(result.replay_failures))
                if result.replay_left:
                    self.window_report(_left_by_replay(result.replay_left))
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
            edit=False,  # the application's own: a scan's result
        )

    def _library_reconciliation_failed(self, generation: int, message: str) -> None:
        """Release one scan barrier after its ordered cleanup could not run."""
        if generation != self._library_scan_generation:
            return
        detail = message or "unknown lifecycle reconciliation failure"
        try:
            if self._window is not None:
                self._window.show_library_scanning(False)
                self._window.report(f"Library result could not be installed: {detail}")
        finally:
            # Whatever the window does (it may be what failed): the barrier
            # ends, or every gesture waiting on this scan would wait forever.
            self._finish_runtime_library_scan(generation, error=detail)
            self._settle_library_refresh_waiters(False)

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
        if self._service_start:
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
        # Last, so a waiter finds the installed library in the window too.
        self._settle_library_refresh_waiters(True)

    def _settle_library_refresh_waiters(self, installed: bool) -> None:
        """Tell every `refresh_library(finished=...)` caller how it ended, once."""
        waiters, self._library_refresh_waiters = self._library_refresh_waiters, []
        for waiter in waiters:
            self._tell_library_refresh_waiter(waiter, installed)

    def _tell_library_refresh_waiter(self, waiter: Callable[[bool], None], installed: bool) -> None:
        """A waiter's failure is its own: it must never read as the scan's."""
        try:
            waiter(installed)
        except Exception as error:  # defensive: the scan paths around this stay classic
            self.window_report(f"After the library refresh: {error}")

    def _begin_runtime_library_scan(self, generation: int) -> None:
        """Make a source-changing scan an explicit publication barrier."""
        sources = self._settings.library_sources
        acquire_lifetime = False
        with self._runtime_config_lock:
            accepted = self._accepted_library_sources
            source_barrier = (
                accepted is None or not self._settings.covers(accepted)
            ) or self._runtime_library_scan_held
            self._runtime_library_scan_generation = generation
            self._runtime_library_scan_sources = sources
            self._runtime_library_scan_pending = True
            self._runtime_library_error = ""
            if source_barrier:
                self._runtime_library_scan_hold_generation = generation
                acquire_lifetime = not self._runtime_library_scan_held
                self._runtime_library_scan_held = True
            # Pending and ready never both hold: a gesture waiting for this scan
            # sleeps on the event until it ends, rather than finding it set and
            # looping for as long as the scan runs.
            self._runtime_library_ready.clear()
        if acquire_lifetime:
            self.hold()

    def _adopt_runtime_library_scan(
        self, generation: int, sources: _LibrarySources | None = None
    ) -> None:
        """Tag an accepted Library before its publication request is captured.

        ``sources``: what that scan read, when its caller knows (a removal's
        own scan, from its plan's settings); else what it was begun for.
        """
        with self._runtime_config_lock:
            if generation != self._runtime_library_scan_generation:
                return
            self._accepted_library_sources = (
                sources if sources is not None else self._runtime_library_scan_sources
            )

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
        if error:
            self._library_scan_failed(error)
        self._mark_removal_scan_terminal(generation, failed=bool(error))
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

    def _library_scan_failed(self, error: str) -> None:
        """A scan meant to replace an invalid inventory failed: say so, and keep it invalid.

        The wallpaper service keeps the last document that was installed;
        nothing new is installed until a scan gets through (a refresh, or the
        next removal's own scan), which publishes as usual.
        """
        with self._runtime_config_lock:
            invalid = not self._inventory_valid
        if not invalid:
            return
        problem = (
            f"The library could not be scanned after a removal ({error}), so nothing new "
            "reaches the wallpaper service until it can be; refresh the library to try again"
        )
        self._inventory_failed(problem)
        self._set_publication_problem(problem)

    def _mark_removal_scan_terminal(self, generation: int, *, failed: bool = False) -> None:
        """Record the bounded post-delete scan tail, including an honest error.

        A failed scan leaves nothing eligible to publish (the inventory stays
        invalid), so the runtime half of the tail ends with it.
        """
        if not self._removal_convergence_held:
            return
        expected = self._removal_convergence_scan_generation
        if expected is None or generation != expected:
            return
        self._removal_convergence_scan_terminal = True
        if failed:
            self._removal_convergence_runtime_terminal = True
        self._maybe_finish_removal_convergence()

    def _update_runtime_document(self) -> tuple[bool, bool]:
        """Synchronously write a live Session for explicit headless callers."""
        if not self._inventory_installable(None):
            with self._runtime_config_lock:
                problem = self._inventory_problem
            self.window_report(problem or "the library is being scanned again")
            return False, False
        try:
            changed = self._installed_leaving_out(self._settings, self._session)
        except (runtime_config.RuntimeConfigError, OSError) as error:
            self.window_report(str(error))
            return False, False
        except Exception as error:  # every outcome is an outcome: never a stranded caller
            self.window_report(f"runtime configuration could not be written: {error}")
            return False, False
        if changed:
            with self._runtime_config_lock:
                self._runtime_document_generation += 1
        self._note_left_out()
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
            # What failed is said; while web wallpapers are off, a page the
            # service still holds must not keep playing (`_web_off_guard`).
            done = self._web_off_guard()
            if done:
                self.window_report(done)
            return False
        if not changed:
            return True
        mark = self._held_mark()
        try:
            response = client.send_runtime("reload")
        except client.NotRunningError:
            self._service_holds_what_is_on_disk()
            self._status_model.mark_unavailable(forget=False)
            return True
        except client.ControlError as error:
            # It may not have loaded it (a reload that timed out): what it holds
            # is judged as for a refusal, as on the GUI's lane.
            failed = f"Runtime reload failed: {error}"
            done = self._web_off_guard()
            self.window_report(f"{failed.rstrip().rstrip('.')}. {done}" if done else failed)
            return False
        if not response.ok:
            refused = self._rejected_configuration(response.message)
            done = self._web_off_guard()
            self.window_report(f"{refused.rstrip().rstrip('.')}. {done}" if done else refused)
            return False
        self._service_holds_what_is_on_disk(mark)
        with self._runtime_config_lock:
            self._runtime_loaded_generation = self._runtime_document_generation
        return True

    def _rejected_configuration(self, message: str) -> str:
        """What is said when the running service refused a new runtime.toml.

        With a service older than this app (between an update and its
        restart), also what ends it.
        """
        return runtime_compatibility.with_restart_hint(
            f"Runtime rejected the new configuration: {message}", self.runtime_status
        )

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
                    self._settings.preview_stills,
                )
            self._runtime_authoring_request = _RuntimeAuthoringRequest(
                self._runtime_authoring_generation,
                self._session.library,
                library_sources,
                self._inventory_stamp,
            )
            self._runtime_publication_hold_target = self._runtime_authoring_generation
            acquire_lifetime = not self._runtime_publication_held
            self._runtime_publication_held = True
        if acquire_lifetime:
            self.hold()
        self._ensure_runtime_compile_queued()
        return True

    def _publish_web_off(self) -> None:
        """Publish web wallpapers going off now, from the library held, whatever its freshness.

        The one path for it, whichever lane publishes (`_publish_runtime_for_context`).
        While web wallpapers are off every web page, and all that a preview could
        have drawn a still for, is left out (`workshop.web_off`), so a library
        scanned for older folders, or under the other preview setting, is safe to
        publish from. Until that publication lands nothing holds one: not a scan in
        flight, one that failed, the save barrier, nor the library's sources
        (`_compile_runtime_request`, `_compile_runtime_until_current_locked`), and
        one held anyway runs the web-off guard (`_stopping_web_after_failure`), as
        one that fails does. A rescan for new sources then publishes again.
        """
        with self._runtime_config_lock:
            # What the lane's next request is numbered (`_publish_runtime_async`).
            self._runtime_web_off_owed = self._runtime_authoring_generation + 1
        try:
            self._publish_runtime_for_context()
        finally:
            with self._runtime_config_lock:
                if self._runtime_authoring_generation < self._runtime_web_off_owed:
                    # Published synchronously (``--service``), or not queued at all:
                    # nothing is owed on the lane.
                    self._runtime_web_off_owed = 0

    def _publish_runtime_for_context(self) -> bool:
        """The GUI's bounded asynchronous path, window or not; synchronous only for ``--service``.

        A GUI process whose last window has closed keeps its accepted tails
        (a removal's convergence, a publication) and finishes them on the
        same path, with the same terminal handling (`_finish_runtime_compile`).
        Only the explicit compatibility service publishes synchronously.
        """
        if self._runtime_shutdown:
            return False
        # From `do_shutdown` on, ``--service`` too: its synchronous publication
        # would hold the loop for as long as the service takes to answer (a
        # reload waits up to RUNTIME_ACTION_TIMEOUT), past the settle's bound
        # (review r13c, G1-L1). On the lane, the shutdown cuts it off; the web-off
        # guard after one it cut is skipped, and the end of the shutdown replaces on
        # disk what it would have (`_stopping_web_after_failure`).
        if not self._service_start or self._shutting_down:
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
        published = False
        try:
            published = self._publish_runtime()
        finally:
            if self._removal_convergence_held:
                # Installed, refused or failed: each is this tail's outcome.
                self._removal_convergence_runtime_terminal = True
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
            and not self._authoring_held
        )

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        """Refuse a GUI mutation until the predecessor decision is settled.

        A refusal is always False, never an exception: callers settle what a
        gesture holds (a queued choice, a disabled control) when refused, so
        saying why must not raise past them. The reason is in the activity log
        before the window is asked to show it.
        """
        if self.authoring_ready:
            return True
        if report:
            try:
                self.window_report(self._migration_blocked_response().message)
            except Exception as error:  # the window may be what failed: refused all the same
                self.activity_log.record("app", f"The window could not show it: {error}")
        return False

    def _migration_blocked_response(self) -> Response:
        if self._quit_requested or self._authoring_held:
            # Not the gate: a quit is under way (`request_quit`, a Ctrl-C's countdown).
            return Response.failure("application is shutting down")
        if self._authoring_gate_state == "checking":
            return Response.failure(
                "current-profile authoring is still opening; retry shortly",
                kind="authoring-busy",
            )
        if self._authoring_gate_state == "repair":
            if self._authoring_gate_reason:
                # The repair could not run at all: the reason, and the file
                # or folder it names, never "dangling playlist references".
                return Response.failure(
                    "current-profile authoring could not be opened, so nothing can be "
                    f"changed until it can: {self._authoring_gate_reason}",
                    kind="authoring-repair-required",
                )
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
        """Invalidate queued authoring and suppress every late socket reply.

        A renderer number handed on (`_AuthoringTask.handed_on`) that is still
        queued, behind another write when the settle ended (its bound, or a
        second Ctrl-C), is still saved: its work is prepared here and started
        on the same worker after the write it waited for, and the process
        waits for that worker before it exits, as for any write already
        running (review r13c, N-2). So are the Settings accepted but not yet
        being written (`_settings_handed_on`), after those numbers, where their
        batch would have queued. Nothing of it reaches the application
        afterwards. Every other queued task is dropped, as before. After a
        Ctrl-C that quit without what is still waiting, the numbers and the
        Settings are dropped too, but for web wallpapers turned off
        (`_quit_without_waiting`, `_drop_waiting_settings`).
        """
        self._authoring_shutdown = True
        queued = tuple(self._authoring_queue)
        self._authoring_queue.clear()
        handed: list[Callable[[], Any]] = []
        for task in queued:
            if (
                task.handed_on
                and not self._quit_without_waiting
                and (not task.requires_migration or self._authoring_migration_ready)
            ):
                attempt = self._prepared_number(task)
                if attempt is not None:
                    handed.append(attempt)
            self._finalize_authoring_task(task)
            self._release_authoring_lifetime(task)
        if self._quit_without_waiting:
            self._drop_waiting_settings()
        settings = self._settings_handed_on()
        if settings is not None:
            handed.append(settings)
        if handed:
            with contextlib.suppress(RuntimeError):
                saving = self._authoring_pool().submit(_save_in_order, tuple(handed))
                if settings is not None:
                    # The settings save the end of the shutdown waits for
                    # (`_turn_off_in_flight`): it lands after the one before it.
                    self._authoring_future = saving
        if self._authoring_jobs is not None:
            # A running atomic store operation owns its own bounded lock/write
            # transaction and must be allowed to reach that durability
            # boundary.  It contains no Session or GTK object, and its late
            # completion is deliberately ineligible to touch the application.
            # The writes handed on above are the only work queued behind it.
            self._authoring_jobs.shutdown(wait=False, cancel_futures=not handed)
            self._authoring_jobs = None

    def _prepared_number(self, task: _AuthoringTask) -> Callable[[], Any] | None:
        """A queued number's work, prepared as at the head of the queue; None when it is gone.

        A wallpaper whose removal committed has no number to save, and is
        dropped quietly, as its timer would drop it (`current_item_for_authoring`).
        """
        try:
            work = task.prepare() if task.prepare is not None else task.work
        except ValueError:
            return None
        except Exception:
            LOGGER.exception("a renderer setting handed on at shutdown could not be prepared")
            return None
        if not task.guard_migration_transaction:
            return work

        def guarded() -> Any:
            with legacy_migration.unattended_transaction():
                return work()

        return guarded

    def _drop_waiting_settings(self) -> None:
        """Drop the Settings accepted and not yet being written, but for web wallpapers off.

        For a quit without the changes still waiting (`_quit_without_waiting`).
        What is left is handed on as ever (`_settings_handed_on`): web
        wallpapers turned off is the safe direction, and is saved, and the end
        of the shutdown judges the file on disk by it, whatever else is dropped.
        What a batch being written took is that batch's, and
        `_settings_handed_on` takes it out, as before.
        """
        with self._settings_authoring_lock:
            pending = self._settings_authoring_pending
            for key in [
                key
                for key, (_accepted, value) in pending.items()
                if not (key == "allow_web_wallpapers" and not value)
            ]:
                del pending[key]
            self._settings_roots_edits = []

    def _settings_handed_on(self) -> Callable[[], Any] | None:
        """The Settings accepted but not yet being written, prepared as their batch; or None.

        For `_shutdown_authoring_jobs`, as `_prepared_number` is for a number.
        A change accepted while a batch is being written (a spin row nudged
        again) waits for that batch's completion to start its own
        (`_finish_settings_batch`), and one whose batch is still queued waits
        for the lane; the shutdown suppresses the one and drops the other, so
        a quit that `request_quit` didn't ask for (SIGINT's ``quit()``) lost
        it (review R41-2). It is prepared here as at the head of the lane
        (`_prepare_settings_batch`), its refusals included, without what the
        batch being written took: that batch writes those itself, and this
        one only what was accepted after it. It turns web wallpapers off if
        either does, for the end of the shutdown (`_turn_off_in_flight`). One
        that is refused is logged and dropped.
        """
        with self._settings_authoring_lock:
            took = self._settings_batch_generation
            turns_web_off = took is not None and self._settings_batch_turns_web_off
            if took is not None:
                pending = self._settings_authoring_pending
                for key in [key for key, (accepted, _value) in pending.items() if accepted <= took]:
                    del pending[key]
                self._settings_roots_edits = [
                    (accepted, edit)
                    for accepted, edit in self._settings_roots_edits
                    if accepted > took
                ]
            if not self._settings_authoring_pending:
                return None
        try:
            prepared = self._prepare_settings_batch()
        except Exception:
            LOGGER.exception("a setting accepted before the shutdown could not be prepared")
            with self._settings_authoring_lock:
                # Only the batch being written is still in flight, as before.
                # What it took stays out of the pending fields: nothing reads
                # them once the shutdown has begun.
                self._settings_batch_generation = took
                self._settings_batch_turns_web_off = turns_web_off
            return None
        with self._settings_authoring_lock:
            self._settings_batch_turns_web_off = self._settings_batch_turns_web_off or turns_web_off
        return prepared

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

    def _quit_waits(self) -> bool:
        """Whether an accepted durable tail hasn't settled yet: what a requested quit waits for."""
        with self._settings_authoring_lock:
            settings_busy = bool(
                self._settings_authoring_running or self._settings_authoring_pending
            )
        return bool(
            self._authoring_active
            or self._authoring_queue
            or self._authoring_lifetime_holds
            or self._runtime_publication_held
            or self._runtime_library_scan_held
            or self._removal_convergence_held
            or settings_busy
        )

    def _maybe_finish_requested_quit(self) -> None:
        """Honor explicit quit only after every accepted durable tail settles."""
        if not self._quit_requested:
            return
        if self._quit_waits():
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
        edit: bool = False,
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
                or (
                    (self._quit_requested or self._authoring_held)
                    and not self._admitted_during_quit(allow_during_quit)
                )
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
                    handed_on=dial_rows.handing_on(),
                    edit=edit,
                )
            )
            self._start_next_authoring_task()

        return server.Deferred(start=start)

    def _start_next_authoring_task(self) -> None:
        """Start a transaction only after the previous one has fully replied, and not while held."""
        if (
            self._authoring_shutdown
            or self._runtime_shutdown
            or self._authoring_active
            or self._authoring_held
            or not self._authoring_queue
        ):
            return
        task = self._authoring_queue.popleft()
        self._authoring_active = True
        self._authoring_current = task
        work = task.work
        if task.requires_migration and not self._authoring_migration_ready:
            try:
                task.reply(self._migration_blocked_response())
            finally:
                self._authoring_task_completed(task)
            return
        if task.prepare is not None:
            try:
                work = task.prepare()
            except Exception as error:
                try:
                    task.reply(server.failed(error))
                finally:
                    self._authoring_task_completed(task)
                return

        def guarded_work() -> Any:
            # A store write that was published but whose folder sync failed is
            # a write (`state_file.published_counts`): collect what it says,
            # for after the task, whatever else happens.
            with (
                state_file.collecting_durability_warnings() as warnings,
                state_file.collecting_writes() as wrote,
            ):
                try:
                    # A later explicit CLI import uses this same marker lock.
                    # Holding it across the complete worker transaction closes
                    # the check/use race between its empty-profile preflight
                    # and target install.
                    if task.guard_migration_transaction:
                        with legacy_migration.unattended_transaction():
                            return work()
                    return work()
                finally:
                    # One folder that can't sync fails every write in it: say it once.
                    task.spoken.warnings.extend(dict.fromkeys(warnings))
                    task.spoken.wrote = bool(wrote)

        try:
            future = self._authoring_pool().submit(guarded_work)
        except RuntimeError:
            try:
                if not self._authoring_shutdown and not self._runtime_shutdown:
                    task.reply(Response.failure("application is shutting down"))
            finally:
                self._authoring_task_completed(task)
            return
        self._authoring_future = future
        # The adoption, the reply and the next queued task all wait on this
        # delivery: above redraw.
        future.add_done_callback(
            lambda done: GLib.idle_add(  # type: ignore[call-arg]
                self._finish_authoring_outcome,
                done,
                task,
                priority=COMPLETION_PRIORITY,
            )
        )

    def _finish_authoring_outcome(
        self,
        future: Future[Any],
        task: _AuthoringTask,
    ) -> bool:
        """Adopt one worker result on GTK, optionally chaining a Deferred.

        The task completes exactly once on every path, a reply that raises
        included, so its lifetime hold and the actor are always released.
        """
        completed = False

        def complete() -> None:
            nonlocal completed
            if not completed:
                completed = True
                self._authoring_task_completed(task)

        def reply(response: Response) -> None:
            # A ``ctl`` caller is told too; the window is told below.
            if task.spoken.warnings:
                said = "; ".join((response.message, *task.spoken.warnings)).lstrip("; ")
                response = Response(ok=response.ok, message=said, kind=response.kind)
            task.reply(response)

        chained = False
        speaking_for, self._speaking_for = self._speaking_for, task
        try:
            if self._authoring_shutdown or self._runtime_shutdown:
                return GLib.SOURCE_REMOVE
            try:
                result = future.result()
            except Exception as error:
                # Refused. If it changed something first (a damaged file moved
                # aside), its caller's "nothing changed" isn't true.
                task.spoken.partly = task.spoken.wrote
                reply(server.failed(error))
                return GLib.SOURCE_REMOVE
            try:
                outcome = task.finish(result)
            except Exception as error:
                reply(self._after_failed_finish(task, error))
                return GLib.SOURCE_REMOVE
            if isinstance(outcome, Response):
                reply(outcome)
                return GLib.SOURCE_REMOVE

            def chained_reply(response: Response) -> None:
                try:
                    if not self._authoring_shutdown and not self._runtime_shutdown:
                        reply(response)
                finally:
                    complete()

            try:
                outcome.start(chained_reply)
                chained = True
            except Exception as error:
                reply(self._after_failed_finish(task, error))
        finally:
            self._speaking_for = speaking_for
            if not self._authoring_shutdown and not self._runtime_shutdown:
                # Whatever the caller said, or didn't: said here on its own.
                self._say_the_rest(task.spoken)
            if not chained:
                complete()
        return GLib.SOURCE_REMOVE

    def _say_the_rest(self, spoken: _Said) -> None:
        """What a task's caller didn't say for it: the lane's message, its warnings."""
        message = ""
        if spoken.instead and not spoken.instead_said:
            spoken.instead_said = True
            message = spoken.instead
        if spoken.warnings and not spoken.said:
            spoken.said = True
            message = _with_durability(message, spoken.warnings)
        if message:
            self.window_report(message)

    def _after_failed_finish(self, task: _AuthoringTask, error: Exception) -> Response:
        """Reply for a task whose adoption on GTK (or what follows it) failed.

        Only a task whose work really changed something on disk
        (`state_file.record_write`) is saved: then it is "Saved, but it
        couldn't be applied: ...", in place of its caller's failure wording
        ("nothing changed"), and what the stores hold is published and shown
        (`_show_what_was_saved`). A task that wrote nothing (a removal of a
        rule that isn't there, say) fails as it always did.
        """
        if not task.spoken.wrote:
            return server.failed(error)
        self._show_what_was_saved()
        detail = str(error) or error.__class__.__name__
        message = f"Saved, but it couldn't be applied: {detail}"
        task.spoken.instead = message
        return Response.failure(message, kind=SAVED_NOT_APPLIED)

    def _show_what_was_saved(self) -> None:
        """Publish and show what the stores hold, after a task's adoption failed partway.

        Never raises: it runs on a path that has already failed once, and the
        reply to the task's caller follows it. A publication that raised is
        the publication problem (`publication_problem`), as a refused one is:
        the service keeps the last setup that worked, the new window says so
        under its header, and the next publication that gets through clears it.
        """
        try:
            self._publish_runtime_for_context()
        except Exception as error:
            detail = str(error) or error.__class__.__name__
            self.activity_log.record("app", f"What was saved could not be published: {detail}")
            try:
                self._set_publication_problem(f"publishing what was saved failed ({detail})")
            except Exception as notice:  # the window may be what failed
                self.activity_log.record(
                    "app", f"The window could not show the publication problem: {notice}"
                )
        window = self._window
        if window is None:
            return
        try:
            window.playlists_changed(self._session)
            window.show_library(self._session)
            window.apply_settings(self._settings)
        except Exception as error:  # the window may be what failed: say it, don't loop
            self.activity_log.record("app", f"The window could not show the saved change: {error}")

    def _authoring_task_completed(self, task: _AuthoringTask) -> None:
        """Release the actor only after mutation, GTK adoption and reply."""
        if self._authoring_current is task:
            self._authoring_current = None
        if task.handed_on and task.spoken.wrote and self._runtime_publication_held:
            # Its finish asked for the publication now held (`pairing_changed`).
            self._number_publication = True
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
        saved_not_applied: Callable[[str], None] | None = None,
        requires_migration: bool = True,
        guard_migration_transaction: bool = True,
        allow_during_quit: bool = False,
        edit: bool = True,
    ) -> bool:
        """Queue one GTK action on the same ordered durability actor.

        The visible completion runs on GTK only after the Store transaction is
        durable.  Errors are delivered as plain text so callers can restore
        their exact controls without allowing worker exceptions to escape the
        main loop.

        Two outcomes are kept apart, explicitly:

        * ``failure`` hears a task that did not save: refused before its
          write, or its work raised (if it changed something first, a damaged
          file moved aside say, what the caller says drops "nothing changed",
          `with_task_durability`). Callers undo there: settle intents, put
          controls back, cancel a batch.
        * A task whose write landed but whose ``finish`` raised
          (`SAVED_NOT_APPLIED`) is never given to ``failure``: undoing a saved
          write's bookkeeping there settles it twice, or cancels a newer
          request (Z-1). The lane publishes and shows what was saved and says
          "Saved, but it couldn't be applied" (`_after_failed_finish`); a
          caller with its own settlement for that case gives
          ``saved_not_applied``.

        ``edit`` is False for the application's own work, never the person's
        change, which a first Ctrl-C then neither counts nor offers to drop
        (`_changes_a_quit_would_drop`).
        """
        if self._authoring_shutdown or self._runtime_shutdown:
            return False
        if (self._quit_requested or self._authoring_held) and not self._admitted_during_quit(
            allow_during_quit
        ):
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
            if response.kind == SAVED_NOT_APPLIED:
                # Committed: never the caller's failure path. Its own
                # settlement if it has one; the lane says the rest.
                if saved_not_applied is not None:
                    saved_not_applied(response.message)
                return
            # Did not save: the caller undoes and says why (made true of the
            # task if it changed something first, `with_task_durability`).
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
            edit=edit,
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
        """Revalidate both sides of a queued representative-still choice.

        Going back to the worked-out still (``requested_still`` None) can leave
        a moving wallpaper with none, and then the compiler drops it: what that
        would break is worked out here, and the worker refuses before the
        write if it finds no still (`runtime_config.pairing_refusals_if_unresolved`).
        """
        current = self.current_item_for_authoring(item)
        still = requested_still
        if still is not None:
            still = self._session.library.require_representative_still(still).path
            if still in self._removed_item_paths:
                raise ValueError(f"{still.name} was removed before the still choice could save")
        store = self._session.pairings
        roots = self._session.library.roots
        # Asked on the worker, of what is saved then: a playlist another
        # program put to use since this window opened is in use.
        unresolved = (
            runtime_config.unresolved_on_what_is_saved(current, self._session)
            if still is None
            else None
        )
        # A chosen still taken from a library that isn't current, or none at
        # all, judged by one: refused while a playlist holds the wallpaper.
        stale = self.pairing_currency_refusal(current)

        def work() -> _StillPairingMutationResult:
            effective = still
            if effective is None:
                effective = pairings.synthesize(
                    current, roots, previews=self._settings.preview_stills
                ).still
            if stale and (still is not None or effective is None):
                raise runtime_config.LibraryNotCurrentError(
                    f"{current.name}\u2019s still was not changed: {stale}"
                )
            broken = unresolved() if effective is None and unresolved is not None else []
            if broken:
                raise runtime_config.PlaybackGuardError(
                    f"{current.name}\u2019s still was not changed: it would have none, and "
                    f"{runtime_config.listed(broken)}"
                )
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
        """Reset and resolve its default away from GTK before acknowledging.

        Refused before the write when the worked-out still is none and that
        would break a playlist (`runtime_config.pairing_refusals_if_unresolved`).
        """
        current = self.current_item_for_authoring(item)
        store = self._session.pairings
        roots = self._session.library.roots
        # Asked on the worker, of what is saved then (see the still choice above).
        unresolved = runtime_config.unresolved_on_what_is_saved(current, self._session)
        stale = self.pairing_currency_refusal(current)

        def work() -> _PairingResetMutationResult:
            effective = pairings.synthesize(
                current, roots, previews=self._settings.preview_stills
            ).still
            if effective is None and stale:
                raise runtime_config.LibraryNotCurrentError(
                    f"{current.name} was not reset: {stale}"
                )
            broken = unresolved() if effective is None else []
            if broken:
                raise runtime_config.PlaybackGuardError(
                    f"{current.name} was not reset: it would have no still, and "
                    f"{runtime_config.listed(broken)}"
                )
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
        # The library is resolved with the choice this process just saved
        # (`Library.chosen_stills`), whether or not the still it plays through
        # changed: choosing the still that was already the automatic one, or
        # resetting to it, must not leave every later guard to scan the disk.
        key = pairings.Identity.of(current).key
        record = self._session.pairings.records.get(key)
        chosen = library.chosen_stills
        if chosen is not None:
            kept = [pair for pair in chosen if pair[0] != key]
            if record is not None and record.still is not None:
                kept.append((key, record.still))
            chosen = tuple(sorted(kept))
        if adopted != current or chosen != library.chosen_stills:
            self._session.adopt_library(
                # Everything else the scan found carried over (`capture_roots` too).
                replace(
                    library,
                    items=tuple(
                        adopted if candidate.path == current.path else candidate
                        for candidate in library.items
                    ),
                    chosen_stills=chosen,
                ),
                reconcile_workshop=False,
            )
        return adopted

    def library_currency_refusal(self) -> str:
        """Why no playlist change may be taken from, or judged by, the library now; or empty.

        The two states in which the compiler refuses to install a document
        built from `session.library` (`_compile_runtime_request`): an
        inventory a pending or committed removal made invalid, until a scan
        after it is accepted (that library still names the removed file on
        purpose), and one that belongs to older library sources (folders or
        Workshop scanning changed, and their scan has not landed). A playlist
        entry taken from it, or a playability check judged by it, could pass
        and leave a playlist with nothing to play once the scan lands, which
        the compiler then builds without (`runtime_config.LeftOut`).
        """
        with self._runtime_config_lock:
            valid = self._inventory_valid
            sources = self._accepted_library_sources
        if not valid:
            return (
                "the library is being scanned again after a removal, so wallpapers can\u2019t "
                "be taken from it yet; wait for that scan to finish (refresh the library if it "
                "failed), then try again"
            )
        # None only before any scan was accepted, by a worker or synchronously
        # (`_library_scanned`): the library is empty, so nothing in it can come
        # from older sources.
        if sources is not None and not self._settings.covers(sources):
            return (
                "the library is being scanned for its changed folders, so wallpapers can\u2019t "
                "be taken from it yet; wait for that scan to finish (refresh the library if it "
                "failed), then try again"
            )
        return ""

    def require_current_library(self) -> None:
        """Refused (`runtime_config.LibraryNotCurrentError`) while the library isn't current.

        `library_currency_refusal` says why. Called from the ``prepare`` of
        every playlist change that takes wallpapers from the library or is
        judged by it, at the head of the authoring lane, so whichever window
        or verb asked, nothing is written.
        """
        refusal = self.library_currency_refusal()
        if refusal:
            raise runtime_config.LibraryNotCurrentError(refusal)

    def pairing_currency_refusal(self, item: MediaItem) -> str:
        """`library_currency_refusal`, when a playlist holds ``item``; else empty.

        For a pairing change that may leave ``item`` with no still: what that
        would break is judged by the library (`runtime_config.pairing_refusal`),
        so it is refused while that library isn't current, unless no playlist
        holds the wallpaper at all.
        """
        refusal = self.library_currency_refusal()
        if not refusal:
            return ""
        held = any(
            entry.path == item.path
            for playlist in self._session.playlists.all()
            for entry in playlist.entries
        )
        return refusal if held else ""

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
            self._status_model.mark_unavailable(forget=True)
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

        # A reply can be the end of an authoring task's Deferred, which holds
        # the authoring lane until it lands.
        GLib.idle_add(deliver, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

    @staticmethod
    def _post_runtime_completion(
        future: Future[_RuntimeResult],
        callback: Callable[[Future[_RuntimeResult]], bool],
    ) -> None:
        """Marshal one runtime-lane completion back onto GTK's main context.

        Every runtime-lane result lands at :data:`COMPLETION_PRIORITY`: at the
        default idle priority (200) it waits below GTK's redraw (120), and an
        animating spinner can starve the very result that would stop it --
        a playback command's busy state, a control reply an authoring task is
        waiting for. One priority for the whole lane also keeps the single
        runtime worker's results in the order it produced them.
        """
        # PyGObject's idle_add override accepts priority=; its stubs omit it.
        GLib.idle_add(callback, future, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

    def _ensure_runtime_compile_queued(self) -> None:
        """Start at most one coalescing compiler job for the latest request.

        Web wallpapers going off are owed a job of their own, request or none
        (f7 G1): a removal's prepare withdraws the request, and the job then
        defers to the removal and runs the guard (`_stopping_web_after_failure`).
        """
        with self._runtime_config_lock:
            if self._runtime_shutdown or self._runtime_compile_pending:
                return
            request = self._runtime_authoring_request
            withdrawn = request is None
            due = self._runtime_web_off_owed != 0 or (
                request is not None
                and (
                    request.generation > self._runtime_compiled_generation
                    or self._runtime_publication_held
                )
            )
            if due:
                self._runtime_compile_pending = True
        if not due:
            if withdrawn:
                self._settle_publication_without_request()
            return
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

    def _compile_runtime_request(self, request: _RuntimeAuthoringRequest) -> bool:
        """Compile one immutable Library plus the latest durable authoring.

        No live application ``Session`` enters this worker.  The short-lived
        session and every one of its stores are constructed and destroyed on
        this thread while the cross-process compiler lock is held. Nothing is
        installed from a Library that isn't a valid inventory now
        (`_inventory_installable`), checked right before the install.

        What the document was built without is kept (`_installed_leaving_out`)
        for GTK to say once the publication settles (`_note_left_out`).
        """
        with (
            legacy_migration.unattended_transaction(),
            runtime_config.compiler_lock(),
        ):
            settings = config.load_strict()
            with self._runtime_config_lock:
                web_off_owed = self._runtime_web_off_owed != 0
            # Web wallpapers going off are published from the library held, whatever
            # sources it was scanned for: while they are off every web page, and all
            # that a preview could have drawn a still for, is left out
            # (`workshop.web_off`), so no library can bring one back. The rescan's own
            # publication follows (`_publish_web_off`).
            if not settings.covers(request.library_sources) and not (
                web_off_owed and not settings.allow_web_wallpapers
            ):
                raise _RuntimeLibraryNotReadyError(
                    "the accepted library belongs to older source settings"
                )
            snapshot = Session(settings)
            try:
                snapshot.adopt_library(request.library, reconcile_workshop=False)
                if not self._inventory_installable(request.inventory):
                    with self._runtime_config_lock:
                        problem = self._inventory_problem
                    raise _RuntimeInventoryInvalidError(
                        problem or "the library is being scanned again"
                    )
                return self._installed_leaving_out(settings, snapshot)
            finally:
                snapshot.shutdown()

    def _installed_leaving_out(self, settings: config.Settings, session: Session) -> bool:
        """`_runtime_installed`, keeping what the document was built without.

        Called with the compiler lock held, or on GTK by the synchronous
        path: the set kept is always the newest compile's, which is the
        document on disk, whatever then becomes of its reload.
        """
        # Held across the store as well as the compile (it is re-entrant), so
        # that the set kept is the newest document's whichever path got there.
        with runtime_config.compiler_lock():
            changed, left_out, replaced = _runtime_installed(settings, session)
            pointer = self._default_pointer(left_out)
            dropped: str | None = None
            with self._runtime_config_lock:
                self._compiled_left_out = left_out
                self._compiled_default_pointer = pointer
                if replaced is not None:
                    self._held_candidates.append((self._held_replaced, replaced))
                    self._held_replaced += 1
                    if len(self._held_candidates) > HELD_CANDIDATES:
                        place, dropped = self._held_candidates.pop(0)
                        self._held_candidates_unknown = True
                        self._held_dropped_through = place + 1
            # Read outside the GTK-shared lock; the compiler lock still orders it
            # before any guard that judges the texts.
            if dropped is not None and runtime_config.allows_web_wallpapers(text=dropped):
                with self._runtime_config_lock:
                    self._held_dropped_allowing = True
        return changed

    def _held_mark(self) -> int:
        """Where the replaced texts stand now: pass it, taken before a reload is sent, to
        `_service_holds_what_is_on_disk` once that reload is confirmed."""
        with self._runtime_config_lock:
            return self._held_replaced

    def _service_holds_what_is_on_disk(self, through: int | None = None) -> None:
        """The service confirmed a reload (or isn't running): it holds the file on disk now.

        ``through``: `_held_mark` as it stood when that reload was sent. Only the
        texts replaced before then are forgotten: one replaced while the reload
        was on the wire may be the very one it loaded (review r13, N2). None:
        every one, for a service that isn't running, or has quit.
        """
        with self._runtime_config_lock:
            if through is None:
                self._held_candidates.clear()
            else:
                self._held_candidates = [
                    (place, text) for place, text in self._held_candidates if place >= through
                ]
            if through is None or self._held_dropped_through <= through:
                self._held_candidates_unknown = False
                self._held_dropped_allowing = False

    @staticmethod
    def _default_pointer(left_out: tuple[runtime_config.LeftOut, ...]) -> str:
        """Where to fix a default that is left out in a ``settings.toml`` that can't be changed.

        Empty otherwise: in a file that can be changed the person can choose
        another default in the app, and the next start clears a dangling one.
        """
        if not any(one.owner == runtime_config.DEFAULT_OWNER for one in left_out):
            return ""
        try:
            why = config.why_settings_cannot_change(config.load_strict_document())
        except config.ConfigError:
            return ""
        if not why:
            return ""
        return (
            f"The settings file can\u2019t be changed: {why}. To change the default, "
            f"edit active_playlist in {paths.settings_path()}."
        )

    def _compile_runtime_until_current(
        self,
        *,
        wait_for_library: bool = False,
    ) -> _RuntimePublication:
        """Serialize runtime publication with the physical removal boundary.

        A job that raises is a failed publication like any other (f7 G9): numbered
        as the newest request, so that web wallpapers going off, owed while it ran,
        get the guard and are owed no longer, and a removal waiting for them
        (`_removal_run`) doesn't wait out its bound. So is a guard that raises
        (f7 H2): its failure is said with the publication's.

        A job that started owing web off and ends owing the same is settled here
        (`_settle_stuck_web_off`), never sent round again (f7 H2).
        """
        with self._runtime_config_lock:
            owed_at_start = self._runtime_web_off_owed
        try:
            with self._runtime_removal_lock:
                publication = self._compile_runtime_until_current_locked(
                    wait_for_library=wait_for_library,
                )
        except Exception as error:  # the lane's worker boundary (`_finish_runtime_compile`'s)
            with self._runtime_config_lock:
                newest = self._runtime_authoring_generation
            publication = _RuntimePublication(newest, error=f"runtime compiler failed: {error}")
        guarded = True
        try:
            publication = self._stopping_web_after_failure(publication)
        except Exception as error:  # the guard's own failure is the publication's (f7 H2)
            guarded = False
            failed = f"the web-off guard failed: {error}"
            publication = replace(
                publication,
                error=f"{publication.error.rstrip().rstrip('.')}; {failed}"
                if publication.error
                else failed,
            )
        with self._runtime_config_lock:
            owed = self._runtime_web_off_owed
            if guarded and 0 < owed <= publication.generation:
                # It landed, or what held it ran the guard: nothing is owed now.
                self._runtime_web_off_owed = owed = 0
            stuck = owed != 0 and owed == owed_at_start
        if stuck:
            self._settle_stuck_web_off(owed)
        return publication

    def _settle_stuck_web_off(self, owed: int) -> None:
        """Web wallpapers going off, owed when a lane job began and still owed when it ended.

        A job that starts owing them publishes them or runs the guard, which ends
        what is owed; one that couldn't (its guard raised, or what is owed is a
        number no request carries) was sent round again by `_finish_runtime_compile`,
        for ever (f7 H2). Here the guard is run once more instead, that is logged,
        and nothing is owed: the next publication carries web off, as every one does.
        """
        LOGGER.warning(
            "web wallpapers going off were still owed (%s) after a runtime job that began "
            "owing them; the web-off guard runs once more",
            owed,
        )
        try:
            if not self._runtime_shutdown:
                self._explaining_web_off_guard()
        except Exception as error:  # said, and not tried again: the lane goes on
            LOGGER.warning("the web-off guard failed again: %s", error)
        with self._runtime_config_lock:
            if self._runtime_web_off_owed == owed:
                self._runtime_web_off_owed = 0

    def _stopping_web_after_failure(self, publication: _RuntimePublication) -> _RuntimePublication:
        """A failed publication or a refused reload, while web wallpapers are off, ends any page.

        The setting is saved before its publication, so a compile or write
        that fails leaves the service on the document it already holds, and
        so does a reload it refuses. When that document may still play a web
        page, `_web_off_guard` ends it for good and the failure says so. Runs
        on the compile lane, never on GTK.

        Not once shutdown has begun (`_runtime_shutdown`). A deliberate quit
        (the last window closed, ``ctl quit`` under ``--service``) gets here
        first: a publication holds the application until it settles, and
        `_maybe_finish_requested_quit` waits for that hold, so this runs for
        it. What is left is work the shutdown itself cut off, by closing its
        socket; the document it installed is on disk for the service's own
        poll of the file, and the next service start is guarded
        (``--service-startup-prepare``). One cut before it installed leaves
        the file it would have replaced, which the end of the shutdown
        replaces with one that plays nothing when it allows web wallpapers
        while they are off (`_web_off_on_disk_at_exit`).
        Socket calls past that point would hold the process's exit for as
        long as they wait.

        Under ``--service`` that includes the publications from `do_shutdown`
        on, which run on this lane too (`_publish_runtime_for_context`), and
        the one the shutdown is for needs no guard (review r13d, N11): the
        publication of a renderer number it handed on. A number doesn't move
        ``allow_web_wallpapers``. Every move of it adopted before
        `do_shutdown` was published synchronously on GTK, this guard
        included (`_publish_runtime`), so the service was left on a document
        that plays no web page while the setting is off, or the guard has
        already ended what it could and said so (a move another program
        saved is its own publication's to guard). A number's publication that
        fails leaves the service on that. One cut at its reload has
        installed what it compiled from the file under the compiler lock,
        which allows no web page while the setting is off
        (`runtime_config.publish_changes`), for the service's poll to load.
        One that wasn't compiled yet installs nothing and fails nothing.
        What is left is a move of the setting saved while the settle's loop
        runs (a gesture in a window still open, a save already under way),
        whose publication, or the number's, which reads the file, the
        shutdown cuts before it installs: the window's case above, which the
        end of the shutdown closes for a file that allows web wallpapers
        (`_web_off_on_disk_at_exit`; one for a service older than schema 8
        is left to that service's next start).
        """
        if self._runtime_shutdown:
            return publication
        if publication.deferred:
            with self._runtime_config_lock:
                owed = self._runtime_web_off_owed != 0
            if not owed:
                return publication
            # Web wallpapers went off and their publication is held after all (a
            # removal in its way): the page ends now, and the publication follows.
            done = self._explaining_web_off_guard()
            return replace(publication, error=done) if done else publication
        if publication.unavailable or (publication.error == "" and publication.response is None):
            if publication.unavailable:
                self._service_holds_what_is_on_disk()
            return publication
        refused = publication.response is not None and not publication.response.ok
        if not publication.error and not refused:
            return publication
        done = self._explaining_web_off_guard()
        if not done:
            return publication
        if refused:
            return replace(publication, note=done)
        return replace(publication, error=f"{publication.error.rstrip().rstrip('.')}. {done}")

    def _explaining_web_off_guard(self) -> str:
        """`_web_off_guard` on a publication lane, what it did kept until it is said.

        A lane's publication carries what the guard did, and only a current one is
        said: one a later request replaced was not, and the person never heard why
        the service stopped or how to start it again (R42-22). So it is also kept
        apart (`_web_off_explained`), said with the next publication that is
        (`_said_with_web_off`), and shown with the publication problem until a
        publication gets through (`publication_problem`).
        """
        done = self._web_off_guard()
        if done:
            with self._runtime_config_lock:
                self._web_off_explained = done
                self._web_off_explained_said = False
        return done

    def _said_with_web_off(self, problem: str) -> str:
        """What a settled publication says: ``problem`` (empty: nothing new), and what the
        web-off guard did, if that hasn't been said yet (`_explaining_web_off_guard`)."""
        with self._runtime_config_lock:
            explained, said = self._web_off_explained, self._web_off_explained_said
            self._web_off_explained_said = True
        if not explained or said or explained in problem:
            return problem
        return f"{problem.rstrip().rstrip('.')}. {explained}" if problem else explained

    def _publication_got_through(self) -> None:
        """A publication reached the service: what the web-off guard did, once said, goes."""
        with self._runtime_config_lock:
            gone = bool(self._web_off_explained) and self._web_off_explained_said
            if gone:
                self._web_off_explained = ""
        changed = getattr(self._window, "publication_changed", None)
        if gone and callable(changed):
            changed()

    def _web_off_guard(self) -> str:
        """End a web page the service may still hold while web wallpapers are off; say what.

        After a publication that failed or a reload the service refused,
        whatever the lane (the compile lane, ``--service``'s synchronous
        one, the health lane). What the service may hold is the file on disk
        or one of the texts publications replaced since it last confirmed a
        reload (`runtime_config.Published.replaced`). Its status says which
        (``config_generation``), so that one alone is judged when it is one of
        them, and whether the one it holds allows web wallpapers
        (``allow_web_wallpapers``), which is taken over any text when it says
        it. Only what lets a page play is ended (review r13, L2):

        * a document that allows web wallpapers (`runtime_config.allows_web_wallpapers`),
          whatever it names: the service's type check then lets a web page through (L4);
        * for a service not confirmed to read schema 8, which has no such
          check, one that plays a web page or a Workshop item whose type
          can't be checked, or one that isn't known any more (past
          `HELD_CANDIDATES`).

        A service that reads 8 and holds a document that doesn't allow web
        wallpapers plays no page: it reads an item's type before it starts
        the engine, and a change of the setting starts every display again.
        Nothing but its status is asked of it then.

        The file on disk is judged the same way when the service doesn't
        hold it (`_unloaded_web_wallpapers`): its own poll of the file loads
        it within about a second, and nothing runs this guard again after
        that (review r13b, D1). A writer that doesn't send its own reload (a
        hand-run ``--write-config`` while web wallpapers were on) leaves one.

        Otherwise the service is told to stop at once, then given a document
        that plays nothing (`runtime_config.nothing_to_play_document`) and
        told to load it: unlike a ``stop``, nothing it is asked afterwards
        (play, toggle, the companion's Resume, its own poll of the file, a
        restart through ExecStartPre) brings the page back. A service that
        can't load that document (one older than this app, still running
        after an update, or one whose status can't be read) is told to quit,
        so that whatever starts it next runs the updated one through its
        ExecStartPre guard. Empty when there is nothing to end or say.

        With no service running nothing plays, but the file on disk is what
        the next one loads: one that allows web wallpapers is replaced as
        above, under the lock (`_web_off_not_running`), so that a start
        already past its own check, or one that doesn't run it, can't load
        it (review r13c, D2).

        The setting is read again once the lock is had (review r13f, N3's
        class): a writer that held it meanwhile may have seen web wallpapers
        turned on again and installed a document for them on, which is then
        what the setting allows, not a page to end.
        """
        try:
            settings = config.load_strict()
        except config.ConfigError:
            return ""  # its own error is said by the compile
        if settings.allow_web_wallpapers:
            return ""
        target = paths.runtime_config_path()
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(runtime_config.compiler_lock())
                locked = True
            except runtime_config.RuntimeConfigError:
                # Another writer holds it past its timeout: judge what is there,
                # stop, and write nothing; that writer's own publication follows.
                locked = False
            try:
                settings = config.load_strict()
            except config.ConfigError:
                return ""
            if settings.allow_web_wallpapers:
                return ""  # turned on again while this waited for the lock
            try:
                on_disk = file_io.read_regular_text(target, runtime_config.MAX_RUNTIME_CONFIG_BYTES)
            except OSError, UnicodeDecodeError:
                on_disk = None
            with self._runtime_config_lock:
                texts = [text for _place, text in self._held_candidates]
                unknown = self._held_candidates_unknown
                dropped_allowing = self._held_dropped_allowing
            if on_disk is not None:
                texts.append(on_disk)
            # Asked whatever the texts say: the document it holds may be one this
            # app never saw replaced (another writer's), and its answer says so.
            try:
                status_reply = client.send_runtime("status")
                decoded = json.loads(status_reply.message) if status_reply.ok else None
            except client.NotRunningError:
                return self._web_off_not_running(settings, on_disk, target, locked=locked)
            except client.ControlError, OSError, ValueError, RecursionError:
                decoded = None
            status = decoded if isinstance(decoded, dict) else None
            holding = self._held_text_loaded(status, texts)
            # The one it holds when that is known; otherwise any of them, and
            # what is no longer known (fail closed).
            judged = [texts[holding]] if holding is not None else texts
            said = status.get("allow_web_wallpapers") if status is not None else None
            if type(said) is bool:
                allows, unsure = said, False
                if holding is None:
                    judged = []  # which text it holds isn't known: only what it said
            else:
                allows = any(
                    runtime_config.allows_web_wallpapers(text=text) for text in judged
                ) or (holding is None and dropped_allowing)
                unsure = holding is None and unknown
            reads_eight = runtime_config.nothing_to_play_supported(status)
            titles, unchecked = runtime_config.web_wallpapers_in(judged)
            held_plays = allows or (not reads_eight and bool(titles or unchecked or unsure))
            unloaded = self._unloaded_web_wallpapers(
                on_disk, status, judged, reads_eight=reads_eight
            )
            if not held_plays and not unloaded:
                return self._web_off_not_reached(titles) if reads_eight else ""
            if held_plays:
                what = self._held_web_words(
                    titles, unchecked, allows=allows, known=status is not None
                )
                lead = f"Allow web wallpapers is off, but the wallpaper service still {what}"
            else:
                # Only the file it hasn't loaded yet: replaced (or the service
                # ended) below as if it held it, so that its poll can't load it.
                lead = (
                    "Allow web wallpapers is off, but the configuration on disk, which the "
                    f"wallpaper service loads the next time it checks the file, {unloaded}"
                )
            # Whether it was playing: the stop below only bridges until the
            # document that plays nothing is loaded, after which Play is put
            # back, so that turning web wallpapers on again plays as before.
            was_playing = isinstance(status, dict) and status.get("playback_state") == "playing"
            try:
                stopped = client.send_runtime("stop").ok
            except client.NotRunningError:
                return self._web_off_not_running(settings, on_disk, target, locked=locked)
            except client.ControlError, OSError:
                stopped = False
            written = False
            mark = 0
            if locked and reads_eight:
                try:
                    document = runtime_config.nothing_to_play_document(
                        settings, runtime_config.WEB_OFF_REASON
                    )
                    if on_disk != document:
                        runtime_config.install_nothing_to_play(document, target)
                    written = True
                except runtime_config.RuntimeConfigNotDurableError as uncertain:
                    state_file.report_not_durable(str(uncertain))
                    written = True
                except runtime_config.RuntimeConfigError:
                    written = False
                if written:
                    with self._runtime_config_lock:
                        self._runtime_document_generation += 1
                        mark = self._held_replaced
        if written:
            try:
                loaded = client.send_runtime("reload").ok
            except client.ControlError, OSError:
                loaded = False
            if loaded:
                self._service_holds_what_is_on_disk(mark)
                with self._runtime_config_lock:
                    self._runtime_loaded_generation = self._runtime_document_generation
                if was_playing:
                    # Nothing can start now; Play only keeps the person's intent.
                    with contextlib.suppress(client.ControlError, OSError):
                        client.send_runtime("play")
                return (
                    f"{lead}. "
                    "Playback was stopped, and the service now has nothing to play: web "
                    "wallpapers are off. Until a publication gets through, no wallpaper is "
                    "played, and the last still Noctalia was given stays on screen."
                )
        if not locked:
            return f"{lead}. " + (
                "Playback was stopped so it doesn\u2019t keep playing, but the service "
                "can start it again (Resume, ctl play) until a publication gets through."
                if stopped
                else "It may still be playing; stop it from the wallpaper service."
            )
        # A service that can't be given nothing to play: end it, as `ctl quit`
        # does. It exits cleanly, so systemd leaves it stopped; the next start
        # (the companion's, or systemctl's) runs ExecStartPre's guard first.
        try:
            quit_ok = client.send_runtime("quit").ok
        except client.NotRunningError:
            quit_ok = True
        except client.ControlError, OSError:
            quit_ok = False
        if quit_ok:
            self._service_holds_what_is_on_disk()
            # Why it couldn't be given nothing to play, as far as is known (N3).
            why = (
                "its status couldn\u2019t be read, so it couldn\u2019t be told to play nothing"
                if status is None
                else "it is older than this app, so it can\u2019t be told to play nothing"
                if not reads_eight
                else "it couldn\u2019t be given a document that plays nothing"
            )
            return (
                f"{lead}. {'It' if held_plays else 'The wallpaper service'} was stopped: "
                f"{why}. Starting it again (systemctl --user start wall-in-one, "
                "or the panel) runs the installed service through its start check, which "
                "doesn\u2019t start it on a configuration that may play a web wallpaper "
                "while they are off."
            )
        if stopped:
            return (
                f"{lead}. "
                "Playback was stopped so it doesn\u2019t keep playing, but the service "
                "can start it again (Resume, ctl play) until a publication gets through."
            )
        return f"{lead}. It may still be playing; stop it from the wallpaper service."

    def _web_off_not_running(
        self,
        settings: config.Settings,
        on_disk: str | None,
        target: Path,
        *,
        locked: bool,
    ) -> str:
        """`_web_off_guard` with no service running: the file on disk is what the next one loads.

        Nothing plays, so nothing is stopped. But ExecStartPre (or the
        companion's own start, which runs the same check) may already be
        past its check, made while web wallpapers were on, and a service run
        by hand runs none. So a document that allows web wallpapers is
        replaced with one that plays nothing (`runtime_config.nothing_to_play_document`),
        exactly as for a running service that reads schema 8: the one it
        replaces is schema 8 too, so whatever could load it loads this. The
        caller holds the compiler lock (``locked``) and read ``on_disk`` under
        it; without the lock, the publication that holds it installs only
        what ``settings.toml`` says now (`runtime_config.publish_changes`).
        Any other document is left: the next start compiles it again, or
        refuses it with exit 78 when it plays a web page
        (``--service-startup-prepare``). Empty when nothing was replaced.
        """
        self._service_holds_what_is_on_disk()
        if not locked or on_disk is None or not runtime_config.allows_web_wallpapers(text=on_disk):
            return ""
        lead = (
            "Allow web wallpapers is off, but the configuration on disk, which the wallpaper "
            "service loads when it starts, may allow web wallpapers"
        )
        try:
            document = runtime_config.nothing_to_play_document(
                settings, runtime_config.WEB_OFF_REASON
            )
            runtime_config.install_nothing_to_play(document, target)
        except runtime_config.RuntimeConfigNotDurableError as uncertain:
            state_file.report_not_durable(str(uncertain))
        except runtime_config.RuntimeConfigError as error:
            return (
                f"{lead}, and it couldn\u2019t be replaced ({error}). The service isn\u2019t "
                "running; its start check doesn\u2019t start it on that configuration while "
                "they are off."
            )
        with self._runtime_config_lock:
            self._runtime_document_generation += 1
        return (
            f"{lead}. The service isn\u2019t running, and that configuration was replaced with "
            "one that plays nothing, until a publication gets through."
        )

    @staticmethod
    def _held_text_loaded(status: dict[str, object] | None, texts: Sequence[str]) -> int | None:
        """Which of ``texts`` the service holds, by its status's ``config_generation``; or None.

        None when its status wasn't read, names no generation, or names one none
        of them has: another writer's (the health sync, a hand edit) that this
        app never saw replaced. Two texts with one generation compile alike.
        """
        generation = status.get("config_generation") if status is not None else None
        if not isinstance(generation, str):
            return None
        for index, text in enumerate(texts):
            try:
                if runtime_config.document_generation(text) == generation:
                    return index
            except runtime_config.RuntimeConfigError:
                continue
        return None

    @staticmethod
    def _unloaded_web_wallpapers(
        on_disk: str | None,
        status: dict[str, object] | None,
        judged: Sequence[str],
        *,
        reads_eight: bool,
    ) -> str:
        """What the file on disk lets play while web wallpapers are off, if it isn't loaded yet.

        Empty when it lets nothing play, or when it is judged already: the
        one the service's status names (``config_generation``), or one of
        ``judged``. Otherwise the service's own poll of the file loads it
        within about a second (review r13b, D1), so it is judged as what it
        holds is: one that allows web wallpapers, under any service; for a
        service not confirmed to read schema 8, one that names a web page or
        a Workshop item whose type can't be checked. The words follow "the
        configuration on disk, which the wallpaper service loads the next
        time it checks the file,".
        """
        if on_disk is None or on_disk in judged:
            return ""
        if Application._held_text_loaded(status, [on_disk]) is not None:
            return ""  # the one it holds: what its status says of it is believed
        allows = runtime_config.allows_web_wallpapers(text=on_disk)
        titles, unchecked = ((), ()) if reads_eight else runtime_config.web_wallpapers_in([on_disk])
        named = ", ".join(titles)
        ids = ", ".join(unchecked)
        if allows:
            allowing = "may allow web wallpapers"
            return f"{allowing}, and names {named}" if titles else allowing
        if titles and unchecked:
            return (
                f"names the web wallpapers {named}, and Workshop wallpapers whose type "
                f"couldn\u2019t be checked ({ids})"
            )
        if titles:
            return f"names the web wallpapers {named}"
        if unchecked:
            return (
                f"names Workshop wallpapers whose type couldn\u2019t be checked ({ids}), so a "
                "web wallpaper can\u2019t be ruled out"
            )
        return ""

    @staticmethod
    def _web_off_not_reached(titles: Sequence[str]) -> str:
        """What is said when the service holds web wallpapers it doesn't play; empty if none.

        A service that reads schema 8, holding a document that doesn't allow
        web wallpapers: it refuses each page when it would start it.
        """
        if not titles:
            return ""
        return (
            "Allow web wallpapers is off, and that hasn\u2019t reached the wallpaper service "
            f"yet: it still holds a configuration that names {', '.join(titles)}. It doesn\u2019t "
            "play them, since that configuration doesn\u2019t allow web wallpapers, so nothing "
            "was stopped."
        )

    @staticmethod
    def _held_web_words(
        titles: Sequence[str],
        unchecked: Sequence[str],
        *,
        allows: bool = False,
        known: bool = True,
    ) -> str:
        """What the held document plays that web wallpapers being off forbids, after "still".

        ``allows``: it allows web wallpapers (`runtime_config.allows_web_wallpapers`).
        ``known``: the service's status was read. Otherwise what it holds, and
        whether it would play it, is only what the app remembers: said as "may".
        """
        if not known:
            if allows:
                return "may hold a configuration that may allow web wallpapers"
            if titles or unchecked:
                held = Application._held_web_words(titles, unchecked)
                return f"may hold a configuration that {held}"
        if allows:
            if titles:
                shown = ", ".join(titles)
                return f"may play {shown}, from a configuration that may allow web wallpapers"
            return "holds a configuration that may allow web wallpapers"
        if titles and not unchecked:
            return f"plays {', '.join(titles)}"
        if unchecked:
            said = (
                f"plays Workshop wallpapers whose type couldn\u2019t be checked "
                f"({', '.join(unchecked)}), so a web wallpaper can\u2019t be ruled out"
            )
            return f"{said}; it also plays {', '.join(titles)}" if titles else said
        return "may hold a document that plays a web wallpaper"

    def _wait_for_library_scan(self, generation: int, changed: bool) -> _RuntimePublication | None:
        """Wait on the runtime lane for the source-changing scan in flight; None: loop again.

        Both of `_compile_runtime_until_current_locked`'s waits are this one: a
        playback gesture's before it compiles, and after a compile the scan's
        library was refused for. Neither holds the lane against what can't wait
        for a scan: web wallpapers going off returns None at once, so the
        caller's loop publishes that, or its guard runs (`_publish_web_off`, R42-9);
        a removal defers to its own outcome, which publishes again; a shutdown
        ends it. A scan that ended in an error is the result, as ``generation``.

        `_runtime_removal_lock`, which the lane holds, is given up while it
        waits: a save that changes the library's sources takes it, and the scan
        waited for is installed on the authoring lane, behind such a save.
        Held, a folder added while a gesture waited (with web wallpapers turned
        off in the same save or not) stopped both lanes until the app quit.
        Nothing is compiled or reloaded while it waits, and the caller's loop
        reads everything again after it.
        """
        self._runtime_removal_lock.release()
        try:
            while not self._runtime_library_ready.wait(0.05):
                with self._runtime_config_lock:
                    if self._runtime_shutdown:
                        return _RuntimePublication(generation, changed=changed)
                    if self._removal_runtime_invalidated:
                        return _RuntimePublication(generation, changed=changed, deferred=True)
                    if self._runtime_web_off_owed:
                        break  # web wallpapers went off: that goes out first
        finally:
            self._runtime_removal_lock.acquire()
        with self._runtime_config_lock:
            scan_error = self._runtime_library_error
        if scan_error:
            return _RuntimePublication(generation, changed=changed, error=scan_error)
        return None

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
                inventory_valid = self._inventory_valid
                inventory_problem = self._inventory_problem
                web_off_owed = self._runtime_web_off_owed != 0
            if shutting_down:
                return _RuntimePublication(max(completed, compiled), changed=changed_any)
            if not inventory_valid and not scan_pending and not removal_pending:
                # Nothing can be installed until a scan replaces the inventory,
                # and none is coming: a terminal result now, on entry or later,
                # whether or not a caller waited through the scan.
                return _RuntimePublication(
                    max(completed, compiled, authoring_generation),
                    changed=changed_any,
                    error=scan_error or inventory_problem or "the library needs a fresh scan",
                )
            if scan_pending and (request is None or wait_for_library) and not web_off_owed:
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
                waited = self._wait_for_library_scan(generation, changed_any)
                if waited is not None:
                    return waited
                continue
            if request is not None and request.generation > compiled:
                try:
                    changed = self._compile_runtime_request(request)
                except _RuntimeLibraryNotReadyError as not_ready:
                    with self._runtime_config_lock:
                        latest = self._runtime_authoring_request
                        scan_pending = self._runtime_library_scan_pending
                        scan_error = self._runtime_library_error
                        removal_pending = self._removal_runtime_invalidated
                        web_off_owed = self._runtime_web_off_owed != 0
                    if latest is not None and latest.generation > request.generation:
                        continue
                    if removal_pending:
                        # The removal's outcome publishes again (its scan after a
                        # commit, or its restore): defer to it, never wait here.
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            deferred=True,
                        )
                    if not scan_pending:
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            error=(
                                scan_error
                                or (
                                    str(not_ready)
                                    if isinstance(not_ready, _RuntimeInventoryInvalidError)
                                    else "The library does not match the saved source settings; "
                                    "refresh it before controlling playback"
                                )
                            ),
                        )
                    if not wait_for_library or web_off_owed:
                        # With web wallpapers off and owed, this request can't be
                        # compiled before the scan ends, and the scan isn't waited
                        # for: deferred, so the web-off guard runs now
                        # (`_stopping_web_after_failure`) and the scan publishes.
                        return _RuntimePublication(
                            request.generation,
                            changed=changed_any,
                            deferred=True,
                        )
                    waited = self._wait_for_library_scan(request.generation, changed_any)
                    if waited is not None:
                        return waited
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
                    removal_invalidated = self._removal_runtime_invalidated
                if invalidated:
                    # A Delete/Trash prepare invalidates the immutable Library
                    # before waiting for this compiler lock. The old compile
                    # may finish, but it is never eligible to reload Rust; a
                    # post-delete scan will replace it with a newer request.
                    if latest is not None and latest.generation > request.generation:
                        if changed and not removal_invalidated:
                            # Merely superseded: what it wrote is on disk, and the
                            # newer request may compile the same document, finding
                            # nothing changed. Counted, so that one's reload sends it
                            # (review-f4: web off was written and never reloaded).
                            changed_any = True
                            with self._runtime_config_lock:
                                self._runtime_document_generation += 1
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
                web_off_owed = self._runtime_web_off_owed != 0
            if latest is not None and latest.generation > completed:
                continue
            # The publication of web wallpapers going off is not held by a scan in
            # flight, nor by one that failed: it is sent now, and the scan's own
            # publication follows.
            if web_off_owed:
                scan_pending = False
                scan_error = ""
            if (
                scan_error
                and latest is not None
                and scan_sources is not None
                and latest.library_sources != scan_sources
                and not self._settings.covers(latest.library_sources)
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
                if not wait_for_library or removal_pending or not scan_pending:
                    # Without a scan to wait for, a generation no request carries
                    # will not be compiled by looping: return, never spin.
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
            if web_off_owed:
                return publication  # web-off went out; the scan's publication follows
            if scan_pending or authoring_generation > completed:
                if not wait_for_library or removal_pending or not scan_pending:
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
            # Or web wallpapers went off while this ran, too late for it, and their
            # request was withdrawn since (a removal's prepare): still owed a job (f7
            # G1). One started while they are owed always clears it, so this ends.
            again = (
                latest is not None and latest.generation > publication.generation
            ) or self._runtime_web_off_owed != 0
            retry_terminal_scan = (
                publication.deferred
                and not self._runtime_library_scan_pending
                and not self._removal_runtime_invalidated
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
                and (
                    self._runtime_compiled_generation >= self._removal_convergence_runtime_target
                    # A terminal failure (a refused or unwritable document) for
                    # the target is an outcome too: nothing new was installed.
                    or (
                        bool(publication.error)
                        and not publication.deferred
                        and publication.generation >= self._removal_convergence_runtime_target
                    )
                )
            ):
                self._removal_convergence_runtime_terminal = True
        try:
            if not self._runtime_shutdown and current:
                problem = ""
                if publication.error:
                    problem = publication.error
                elif publication.unavailable:
                    self._status_model.mark_unavailable(forget=False)
                elif publication.response is not None and not publication.response.ok:
                    problem = self._rejected_configuration(publication.response.message)
                    if publication.note:
                        problem = f"{problem.rstrip().rstrip('.')}. {publication.note}"
                elif publication.changed and self._window is not None:
                    self.refresh_runtime_status_async()
                said = self._said_with_web_off(problem)
                if said:
                    self.window_report(said)
                if not publication.deferred:
                    self._set_publication_problem(problem)
                    if not problem and not publication.unavailable:
                        self._publication_got_through()
                    self._note_left_out()
        finally:
            # Whatever reporting did: the next request, the publication hold
            # and the removal's tail are always serviced.
            if (again or retry_terminal_scan) and not self._runtime_shutdown:
                self._ensure_runtime_compile_queued()
            if publication_hold_terminal:
                self._release_runtime_publication_hold()
            self._settle_publication_without_request()
            self._maybe_finish_removal_convergence()
        return GLib.SOURCE_REMOVE

    @property
    def publication_problem(self) -> str:
        """Why the last publication didn't reach the wallpaper service, or empty.

        The compiler's refusal (a store file it could not read, naming it, or
        a limit of the runtime's) or the runtime's own; the service keeps the
        last configuration that worked meanwhile. The next publication that
        gets through clears it. A playlist that can't play is never this: the
        document is published without what names it (`left_out_notice`). What
        the web-off guard did on a lane is said with it until then too, whatever
        later publications fail with (`_explaining_web_off_guard`, R42-22).
        """
        problem, explained = self._publication_problem, self._web_off_explained
        if not explained or explained in problem:
            return problem
        return f"{problem.rstrip().rstrip('.')}. {explained}" if problem else explained

    @property
    def left_out_notice(self) -> str:
        """What the published runtime document was built without, in words; or empty.

        A default, a schedule rule or a display's own playlist that names a
        playlist which doesn't exist or has nothing to play is left out by
        the compiler, and everything else is published
        (`runtime_config.LeftOut`). It stands for as long as that is so: the
        new window keeps it under its header, below a publication problem.
        """
        notice = runtime_config.left_out_notice(self._left_out_said)
        if notice and self._default_pointer_said:
            return f"{notice} {self._default_pointer_said}"
        return notice

    def _note_left_out(self) -> None:
        """Say what the newest compiled document leaves out, if that has changed.

        On GTK, after a publication settles: the compile lane's, the health
        lane's or a synchronous one. Said once, in the window's toast and the
        activity log (`window_report`), and again only when the set of
        references left out is a different one; never by every publication or
        status poll that meets the same set. When nothing is left out any
        more the notice goes, and the log says so.
        """
        with self._runtime_config_lock:
            compiled = self._compiled_left_out
            pointer = self._compiled_default_pointer
        if compiled is None:
            return
        left_out = frozenset(compiled)
        if left_out == self._left_out_said and pointer == self._default_pointer_said:
            return
        self._left_out_said = left_out
        self._default_pointer_said = pointer
        if left_out:
            self.window_report(self.left_out_notice)
        else:
            self.activity_log.record("app", "Nothing that playback uses is left out any more.")
        changed = getattr(self._window, "publication_changed", None)
        if callable(changed):
            changed()

    def _set_publication_problem(self, problem: str) -> None:
        if problem == self._publication_problem:
            return
        self._publication_problem = problem
        # The new window keeps it under its header; the classic one says it
        # in its toast, as it did.
        changed = getattr(self._window, "publication_changed", None)
        if callable(changed):
            changed()

    def _settle_publication_without_request(self) -> None:
        """End a publication hold whose request was withdrawn and that nothing will publish.

        A removal's prepare withdraws the pending request (its Library may
        name what the removal deletes). When the removal then publishes
        nothing (an inventory restored invalid, scans that failed), no compile
        is coming for that hold: nothing eligible to publish is its outcome.
        A later publication takes a hold of its own.
        """
        with self._runtime_config_lock:
            orphaned = (
                self._runtime_publication_held
                and self._runtime_authoring_request is None
                and not self._runtime_compile_pending
                and not self._removal_runtime_invalidated
                and not self._runtime_library_scan_pending
            )
        if orphaned:
            self._release_runtime_publication_hold()

    def _release_runtime_publication_hold(self) -> None:
        """Balance the coalesced hold after its newest terminal publication."""
        if not self._runtime_publication_held:
            return
        self._runtime_publication_held = False
        self._runtime_publication_hold_target = 0
        self._number_publication = False
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

    def check_setup_again(self) -> bool:
        """Setup's Check Again: compile ``runtime.toml`` as a save does, then reload the service.

        A renderer installed since the last compile is a new path in the
        document on Nix (a profile's ``bin``), and on Arch the very path the
        document already named, so the compile may change no byte. The reload
        therefore goes whether or not it did: it is what makes the service try
        a renderer it found missing again (docs/missing-components.md). The
        compile is the window's own publication path, when authoring is
        ready; the reload follows it on the same ordered lane. Its answer
        reaches the window through the next status, as any reload's does.
        """
        if self._runtime_shutdown:
            return False
        if self.authoring_ready:
            self._publish_runtime_async()
        try:
            future = self._runtime_pool().submit(self._reload_for_setup_check)
        except RuntimeError:
            return False
        future.add_done_callback(
            lambda done: self._post_runtime_completion(done, self._finish_setup_reload)
        )
        return True

    def _reload_for_setup_check(self) -> Response | None:
        """One unconditional ``reload``; None when no service listens."""
        try:
            return self._send_gui_runtime("reload")
        except client.NotRunningError:
            return None

    def _finish_setup_reload(self, future: Future[Response | None]) -> bool:
        if self._runtime_shutdown:
            return GLib.SOURCE_REMOVE
        try:
            response = future.result()
        except Exception as error:  # a socket error, or anything else on the lane
            self.window_report(f"Runtime reload failed: {error}")
            return GLib.SOURCE_REMOVE
        if response is None:
            self._status_model.mark_unavailable(forget=False)
        elif not response.ok:
            self.window_report(self._rejected_configuration(response.message))
        if self._window is not None:
            self.refresh_runtime_status_async()
        return GLib.SOURCE_REMOVE

    def _finish_runtime_reload(self, future: Future[Response]) -> bool:
        """Finish a coalesced authoring reload on GTK's thread."""
        self._runtime_reload_pending = False
        if not self._runtime_shutdown:
            try:
                response = future.result()
            except client.NotRunningError:
                self._status_model.mark_unavailable(forget=False)
            except client.ControlError as error:
                self.window_report(f"Runtime reload failed: {error}")
            else:
                if not response.ok:
                    self.window_report(self._rejected_configuration(response.message))
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
                mark = self._held_replaced
            if loaded >= wanted:
                return Response.success()
            response = self._send_gui_runtime("reload")
            if not response.ok:
                return response
            self._service_holds_what_is_on_disk(mark)
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
        window: WindowServices,
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
                self._status_model.mark_unavailable(forget=True)
        except client.ControlError:
            # A missed deadline is not proof that nobody owns the runtime.
            # Retain the last atomic answer rather than making the header and
            # authoring pages jump to Python's stale Session state. Crucially,
            # no compatibility callback is allowed to start a second driver.
            if current:
                self._status_model.mark_delayed()
        else:
            if current:
                if not reply.response.ok:
                    self._status_model.mark_invalid(
                        f"Runtime rejected its status request: {reply.response.message}"
                    )
                elif reply.protocol_error:
                    self._status_model.mark_invalid(reply.protocol_error)
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
            self._status_model.mark_unavailable(forget=True)
            return False
        except client.ControlError:
            # A timeout or malformed answer is not proof that the process is
            # absent.  In particular, the retained Python fallback must not
            # start applying wallpapers merely because a busy Rust runtime
            # missed one status deadline.
            self._status_model.mark_delayed()
            return True
        if not response.ok:
            self._status_model.mark_invalid(
                f"Runtime rejected its status request: {response.message}"
            )
        else:
            try:
                status: object = json.loads(response.message)
            except ValueError, RecursionError:
                self._status_model.mark_invalid(
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
                    self._status_model.mark_invalid(
                        "Runtime returned a status reply without playlist state"
                    )
        # Even a rejected status request proves that this socket has an owner;
        # do not turn a protocol failure into permission for a second driver.
        return True

    def _adopt_runtime_status(
        self,
        status: dict[str, object],
        window: WindowServices | None,
        *,
        health_document: str | None = None,
    ) -> None:
        """Show one atomic snapshot and queue guarded health persistence.

        Missing reports never clear health.  Mapping entries, taking the
        compiler lock, reloading stores, rendering a large document and
        fsyncing Pairings all belong to the ordered worker, never GTK.

        ``window`` is the application's current window, or None when there is
        none; it chooses how health is persisted. The snapshot reaches that
        window through the status model's forwarding subscriber.
        """
        omitted = status.get("taboo_entries_omitted", 0)
        if type(omitted) is int and omitted > self._taboo_omitted_seen:
            self._taboo_omitted_seen = omitted
            self.window_report(
                f"The runtime has {omitted} older playback warnings not included in this "
                "status update; existing saved warnings were retained"
            )
        # After the report above, which used to precede the window's render.
        self._status_model.adopt(status)
        self._publish_again_when_presets_become_readable(status)
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
        with self._runtime_config_lock:
            inventory = self._inventory_stamp
        request = _RuntimeHealthRequest(
            authoring_generation,
            self._session.library,
            health_document,
            inventory,
        )
        if window is None:
            self._apply_runtime_health_result(self._ingest_runtime_health(request))
            return
        self._queue_runtime_health(request)

    def _publish_again_when_presets_become_readable(self, status: dict[str, object]) -> None:
        """Presets or dials left out for a service that didn't read them: publish again.

        Once it does. The notice stands until the next publication, which the
        GUI would otherwise not make after the service restarted. Only the
        change to "reads presets" (or the dials) asks, so units that can't be
        verified (which leave them out again) are not asked over and over.
        So does ``allow_web_wallpapers`` left out for a service that lists 8
        but isn't confirmed (`runtime_config.WEB_OWNER`), as the dials do; and
        a service started again (a new ``runtime_instance``, as the notice
        asks after its units are made current) is asked once more.
        """
        reads = runtime_config.presets_supported(status)
        newly = reads and not self._presets_readable_seen
        self._presets_readable_seen = reads
        # The renderer dials too: left out the same way, for schema 7.
        dials = runtime_config.dials_supported(status)
        newly = newly or (dials and not self._dials_readable_seen)
        self._dials_readable_seen = dials
        eight = runtime_config.nothing_to_play_supported(status)
        instance = status.get("runtime_instance")
        if isinstance(instance, str):
            if self._runtime_instance_seen is not None and instance != self._runtime_instance_seen:
                self._left_out_retried = False
            self._runtime_instance_seen = instance
        # Left out of the last publication, which compiled without a
        # confirmation (a status that timed out, say, or units that couldn't
        # be verified), while this status lists what was left out: ask for it
        # again, once. A service that lists it but whose units still can't be
        # verified leaves it out again, and is not asked a second time until
        # it is no longer waiting for that, or is started again.
        waiting = any(
            one.why == runtime_config.NEEDS_NEWER_SERVICE
            and (
                eight
                if one.owner == runtime_config.WEB_OWNER
                else dials
                if one.owner == runtime_config.DIALS_OWNER
                else reads
            )
            for one in self._left_out_said
        )
        retry = waiting and not self._left_out_retried
        self._left_out_retried = waiting and (self._left_out_retried or retry)
        # Schema 8, once a service lists it (the updated one, after an older one
        # was told to quit): a publication that couldn't be given to the older
        # one (a playlist that plays nothing), or one that left out
        # `allow_web_wallpapers` for it, is made again, once.
        newly_eight = eight and not self._nothing_readable_seen
        self._nothing_readable_seen = eight
        if newly_eight and (self._publication_problem or self._settings.allow_web_wallpapers):
            self._publish_runtime_for_context()
            return
        if (newly or retry) and any(
            one.why == runtime_config.NEEDS_NEWER_SERVICE for one in self._left_out_said
        ):
            self._publish_runtime_for_context()

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
            edit=False,  # the application's own: what the service reported
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
                    or runtime_config.document_generation(
                        # What was published for this service: without
                        # presets or the dials when it doesn't read them
                        # or its units can't be verified.
                        runtime_config.compiled_as_published(
                            settings, snapshot, status, status_generation
                        ).document
                    )
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

                if not self._inventory_installable(request.inventory):
                    # The markers are saved; the next valid publication carries them.
                    return _RuntimeHealthResult(request, saved=changed > 0)
                try:
                    document_changed = self._installed_leaving_out(settings, snapshot)
                except runtime_config.RuntimeConfigError as error:
                    # The markers are saved whatever the compiler makes of
                    # them: adopt them, and say why this publication stopped
                    # as every other one does (`_apply_runtime_health_result`).
                    return _RuntimeHealthResult(
                        request,
                        changed=changed,
                        accepted=True,
                        store=snapshot.pairings,
                        publication=_RuntimePublication(
                            request.authoring_generation, error=str(error)
                        ),
                        saved=changed > 0,
                    )
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
            if publication is not None:
                # A refused reload here leaves what the service held, as on
                # the compile lane: while web wallpapers are off, no page.
                publication = self._stopping_web_after_failure(publication)
            return _RuntimeHealthResult(
                request,
                changed=changed,
                accepted=True,
                store=snapshot.pairings,
                publication=publication,
                saved=changed > 0,
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
        """Apply a completed health transaction without any filesystem work.

        A status poll asks for the same transaction again every two seconds,
        so what it could not do is said once, not on every poll: a save that
        failed through `_say_health_failure_once`, forgotten once a save lands
        (`_RuntimeHealthResult.saved`); a publication that failed when it
        becomes the publication problem. A transaction that tried nothing
        (the gate was busy, the status was stale, the markers were there
        already) is neither a failure nor a success, and leaves both as they are.
        What a document was built without is said once too (`_note_left_out`).
        """
        if result.error:
            self._say_health_failure_once(result.error)
            return
        if result.saved:
            self._runtime_health_said = None
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
        # What this lane's own compile left out, if it compiled: said only when
        # it is not what was said last, so no poll repeats it.
        self._note_left_out()
        publication = result.publication
        if publication is None:
            return
        # The same publication-problem path as the compile lane's
        # (`_finish_runtime_compile`): said, and kept until one gets through.
        problem = ""
        if publication.error:
            problem = publication.error
        elif publication.unavailable:
            self._status_model.mark_unavailable(forget=False)
        elif publication.response is not None and not publication.response.ok:
            problem = self._rejected_configuration(publication.response.message)
            if publication.note:
                problem = f"{problem.rstrip().rstrip('.')}. {publication.note}"
        # Said when it becomes the problem, not by every poll that meets it
        # again: it stands (`publication_problem`) until a publication gets
        # through, this lane's or the compile lane's, and is new after that.
        said = self._said_with_web_off(problem if problem != self._publication_problem else "")
        if said:
            self.window_report(said)
        self._set_publication_problem(problem)
        if not problem and not publication.unavailable:
            self._publication_got_through()

    def _say_health_failure_once(self, message: str) -> None:
        """Report a health save that failed, unless that was just said.

        For as long as the runtime reports a wallpaper it couldn't play,
        every status poll (one each two seconds) asks for the same save, and
        what refused one refuses the next the same way: a read-only state
        folder said "Could not save wallpaper playback availability" in a
        toast and in the log on every poll, without end. So what was last
        said is remembered, and said again only when it reads differently
        (another file, another error: this is no mute), after a save landed
        in between (`_apply_runtime_health_result`), or to a window that
        wasn't there when it was said.
        """
        said = (self._window_generation, message)
        if said != self._runtime_health_said:
            self._runtime_health_said = said
            self.window_report(message)

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
        self._status_model.set_busy(True)
        try:
            future = self._runtime_pool().submit(self._execute_gui_runtime_call, work)
        except RuntimeError:
            self._runtime_action_pending = False
            self._status_model.set_busy(False)
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

            # It ends the busy state (the new player bar's spinner).
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
        window: WindowServices | None,
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
            self._status_model.mark_unavailable(forget=True)
        try:
            if response.ok and on_success is not None:
                on_success(runtime_answered)
        finally:
            # Released whatever the callback did (it reaches the window): the
            # single-flight flag, the caller's own completion, and busy below.
            self._runtime_action_pending = False
            try:
                if on_complete is not None:
                    on_complete(response, runtime_answered)
            finally:
                if not self._runtime_shutdown:
                    self._status_model.set_busy(False)
        current = (
            not self._runtime_shutdown
            and window is not None
            and self._window is window
            and self._window_generation == generation
        )
        # A command belongs to the application, not to one incarnation of
        # its window. The single-flight busy flag was released above for
        # whichever window is open now; only the result stays away from a
        # reopened one.
        if current and window is not None:
            if not response.ok:
                window.report(response.message)
            self.refresh_runtime_status_async()
        elif not self._runtime_shutdown and self._window is not None:
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
            self._status_model.mark_unavailable(forget=True)
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
            self.require_current_library()
            runtime_config.refuse_unresolved_quick_choice(item, self._session)
        except (runtime_config.PlaybackGuardError, runtime_config.LibraryNotCurrentError) as error:
            return server.failed(error)
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
            self.require_current_library()
            current = self._session.library.find(item.path)
            if current is None or current.path in self._removed_item_paths:
                raise ValueError(f"{item.name} was removed before Quick choice could save")
            health = self._session.pairings.health(pairings.Identity.of(current))
            if health.is_borked:
                raise ValueError(f"Playback unavailable for {current.name}")
            runtime_config.refuse_unresolved_quick_choice(current, self._session)
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
            # Saved: released first, before anything here that can raise
            # (`playlists_changed` reaches the window). What follows the save
            # is the runtime call's, which holds its own flag.
            release_choice()
            try:
                self._session.use_playlist(chosen.id)
            except ApplyError as error:
                # Saved (a health marker landed meanwhile, say): published and
                # said as saved, never as a choice that didn't happen.
                self.playlists_changed()
                self.window_report(f"Saved, but it couldn't be applied: {error}")
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
            self.require_current_library()
            current = self._session.library.find(item.path)
            if current is None or current.path in self._removed_item_paths:
                raise ValueError(f"{item.name} was removed before Quick choice could save")
            health = self._session.pairings.health(pairings.Identity.of(current))
            if health.is_borked:
                raise ValueError(f"Playback unavailable for {current.name}")
            runtime_config.refuse_unresolved_quick_choice(current, self._session)
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
            # Saved: released first, before anything here that can raise
            # (`playlists_changed` reaches the window). What follows the save
            # is the runtime call's, which holds its own flag.
            release_choice()
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
        # The first root whose still directory no Workshop item can write: one made
        # in another would never be found (f7 W1). The scan's verdict, read: this
        # runs on GTK at the end of every scan (`pairing.library_capture_root`, f7 G2).
        root = pairing.library_capture_root(self._session.library)
        if root is None:
            return
        # A web page while they are off is never played, so it gets no still.
        allowed = self._settings.allow_web_wallpapers
        wanted = [
            item for item in self._session.library.items if not workshop.web_off(item, allowed)
        ]
        self._stills.request(wanted, root, self._on_stills_made)

    def regenerate_scene_still(self, item: MediaItem) -> bool:
        """Queue an explicit scene recapture; return whether it could start."""
        if not self.require_authoring_ready():
            return False
        if not self._settings.roots:
            self.window_report("Choose a library folder in Settings before generating stills")
            return False
        root = pairing.library_capture_root(self._session.library)  # read, not looked up (G2)
        if root is None:
            # Folders there are, but none a still can go in (f7 G4).
            self.window_report(NO_STILLS_FOLDER)
            return False
        if not item.scene or workshop.no_automatic_still(item, self._settings.preview_stills):
            return False
        if workshop.why_not_played(item, self._settings.allow_web_wallpapers):
            return False
        self._stills.regenerate_scene(item, root, self._on_stills_made)
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

    def _removal_run(self, plan: RemovalPlan) -> Callable[[], RemovalResult]:
        """``plan``'s run on the authoring worker, the runtime lane and the compiler lock held.

        Web wallpapers going off get out first (f6 L2). Their save landed before
        this removal, queued behind it, was prepared, and their publication, or
        the guard it runs (`_stopping_web_after_failure`), needs the lane and the
        compiler lock this run holds throughout. Taken first, they waited for the
        whole removal. So the run waits, before it takes either, until nothing is
        owed (`_runtime_web_off_owed`), the application shuts down, or
        `REMOVAL_WEB_OFF_WAIT_SECONDS` pass; no save can land meanwhile, being on
        this same worker. The ``ctl`` removal runs this too.
        """

        def run() -> RemovalResult:
            deadline = time.monotonic() + REMOVAL_WEB_OFF_WAIT_SECONDS
            while time.monotonic() < deadline:
                with self._runtime_config_lock:
                    if not self._runtime_web_off_owed or self._runtime_shutdown:
                        break
                time.sleep(0.05)
            with (
                self._runtime_removal_lock,
                legacy_migration.unattended_transaction(),
                runtime_config.compiler_lock(),
            ):
                return plan.run()

        return run

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
            # Whether a playlist would break is the removal's own dry run, on
            # the worker before anything is touched (`RemovalPlan.run`,
            # `wall_in_one.dry_run.removal`): the real scan, cleanup and
            # compiler, so a rule, assignment or default saved after the
            # gesture is seen, and so is every still another wallpaper plays
            # through.
            plan = self._session.prepare_removal_plan(current, trash=trash)
            self._invalidate_runtime_for_pending_removal()
            self._removal_active = True
            return self._removal_run(plan)

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
            # A playback gesture can be waiting on this scan: above redraw.
            GLib.idle_add(self.refresh_library, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

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

    def _adopt_post_removal_scan(
        self, library: Library, sources: _LibrarySources, *, refresh_follows: bool
    ) -> None:
        """Install the scan a removal ran once it had committed (`RemovalResult.own_scan`).

        That is the real post-removal library, so it is installed as a finished
        scan is (`refresh_library` without a window): adopted, its sources
        accepted, shown and published, and the runtime's library barrier and
        the removal's convergence released at its generation. Nothing scanned
        before the commit is installed: the removal's dry run decides whether
        it may go ahead, and only that. Only a scan of the sources the
        settings name now gets here (`_adopt_item_removal`). With
        ``refresh_follows`` (a removal that committed with a failure, or a
        refresh asked for meanwhile), it is adopted and shown, and the refresh
        that follows owns the barrier, the convergence and the publication.
        """
        generation = self._library_scan_generation
        before = self._session.library
        # Whatever it took away, under any spelling (an alias through a linked
        # folder too), stays taboo until a later scan finds it again.
        kept = {item.path for item in (*library.items, *library.still_inventory)}
        self._removed_item_paths.update(
            item.path for item in (*before.items, *before.still_inventory) if item.path not in kept
        )
        self._session.adopt_library(library, reconcile_workshop=False)
        if refresh_follows:
            # The refresh that follows is the inventory, once it lands.
            if self._window is not None:
                self._window.show_library(self._session)
            return
        self._validate_inventory()
        self._removal_convergence_scan_generation = generation
        # What the worker scanned (its plan's settings), taken from the scan
        # itself; `_adopt_item_removal` let it through only while the
        # settings still name the same sources.
        self._adopt_runtime_library_scan(generation, sources)
        try:
            self._finish_library_refresh(scan_generation=generation)
        finally:
            # The scan barrier and the removal's scan half end, whatever showing
            # or publishing the library did.
            self._finish_runtime_library_scan(generation)

    def _adopt_item_removal(self, result: RemovalResult) -> None:
        """Install only semantic worker deltas after its lease is released.

        A committed removal's library is the one a real scan found after the
        commit, on the same worker (`RemovalResult.own_scan`); until it is
        installed, nothing new is published and the previous generation
        stays. A removal that committed with a failure, or a refresh asked for
        meanwhile, is scanned again (with the pending-removal replay), and so
        is one whose own scan failed, came back without its sources, or read
        sources the settings no longer name. Until that scan lands the
        inventory stays invalid, so the guard and the compiler refuse.
        """
        self._removal_active = False
        if result.committed:
            # The file is gone: from here on, nothing may restore what was in
            # force before the removal (`_restore_runtime_after_uncommitted_removal`),
            # so this comes first, before anything that could fail.
            # Invalid from the commit until a scan after it is accepted, whatever
            # happened since its prepare.
            self._invalidate_inventory(
                f"{result.item.name} was removed; the library is being scanned again"
            )
            self._removed_item_paths.add(result.item.path)
            for cleanup in result.cleanups:
                self._removed_item_paths.update(cleanup.removed_stills)
            self._invalidate_library_scan_after_removal()
            self._finish_runtime_removal_invalidation()
        try:
            self._adopt_removal_deltas(result)
        except Exception:
            if result.committed:
                # Adopting the rest failed: the inventory stays invalid, so the
                # guards refuse, until the scan the lane starts once it is free
                # lands (`_release_removal_lane`). The lane says it saved.
                self._refresh_after_removal = True
            raise

    def _adopt_removal_deltas(self, result: RemovalResult) -> None:
        """What `_adopt_item_removal` takes in after the commit's own bookkeeping."""
        self._session.adopt_library_cleanups(result.cleanups)
        self._session.adopt_library_fault_repairs(result.repaired_faults)
        partial = bool(result.error_message or result.cleanup_failures)
        # Its own scan, only with the sources it read and only while they are
        # still the settings' (as the guard compares them): a scan of folders
        # the settings no longer name is no inventory, so it is not installed
        # and the rescan below replaces it, as one that failed would be.
        own_scan = result.own_scan
        if own_scan is not None and not self._settings.covers(own_scan[1]):
            own_scan = None
        refresh = self._refresh_after_removal or (
            result.committed and (partial or own_scan is None)
        )
        self._refresh_after_removal = False
        if result.committed:
            self._stills.forget(result.item.path)
            self._begin_removal_convergence()
            if own_scan is not None:
                scanned, sources = own_scan
                self._adopt_post_removal_scan(scanned, sources, refresh_follows=refresh)
            # Otherwise the rescan below is the post-removal library. A new
            # detached refresh starts only after the removal worker has
            # released its operation lease and the actor has adopted every
            # semantic cleanup delta.
        if refresh:
            # The removal cleared the library barrier above, so a playback
            # gesture -- spinner and all -- waits for this scan to start and
            # finish: above redraw, or its own animation could starve it.
            GLib.idle_add(self.refresh_library, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

    def _invalidate_inventory(self, problem: str) -> None:
        """From now on `session.library` may name files that are gone: install nothing from it."""
        with self._runtime_config_lock:
            self._inventory_stamp += 1
            self._inventory_valid = False
            self._inventory_problem = problem

    def _inventory_failed(self, problem: str) -> None:
        """Say why the inventory stays invalid (a scan that should have replaced it failed)."""
        with self._runtime_config_lock:
            if self._inventory_valid:
                return
            self._inventory_problem = problem

    def _validate_inventory(self) -> None:
        """`session.library` is a fresh inventory again; its publication follows at once."""
        with self._runtime_config_lock:
            if self._inventory_valid:
                return
            self._inventory_stamp += 1
            self._inventory_valid = True
            self._inventory_problem = ""

    def _inventory_installable(self, stamp: int | None) -> bool:
        """Whether a document built from a Library captured at ``stamp`` may be installed."""
        with self._runtime_config_lock:
            return self._inventory_valid and (stamp is None or stamp == self._inventory_stamp)

    def _invalidate_runtime_for_pending_removal(self) -> None:
        """Cancel stale health/compile requests before physical deletion.

        The removal's own barrier is ``_removal_runtime_invalidated`` (with
        the inventory invalid): publications defer and gestures fail promptly
        until it resolves. The scan lifecycle is left to the scan worker, so
        a refresh that completes meanwhile, however it ends, still counts.
        """
        with self._runtime_config_lock:
            self._inventory_before_removal = (self._inventory_valid, self._inventory_problem)
        self._invalidate_inventory("a wallpaper is being removed")
        with self._runtime_config_lock:
            self._runtime_authoring_generation += 1
            self._runtime_authoring_request = None
            self._runtime_health_request = None
            self._removal_runtime_invalidated = True

    def _invalidate_library_scan_after_removal(self) -> None:
        """Forbid a pre-delete scan from masquerading as a reinstall."""
        self._library_scan_generation += 1
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
            if self._runtime_library_scan_held:
                # The commit cancelled the source-changing scan holding this
                # barrier (`_invalidate_library_scan_after_removal`); the scan
                # after the commit replaces it, and ends the hold with it.
                self._runtime_library_scan_hold_generation = self._library_scan_generation
            self._runtime_library_scan_generation = self._library_scan_generation
            self._runtime_library_scan_sources = self._settings.library_sources
            self._runtime_library_scan_pending = True
            self._runtime_library_error = ""
            self._runtime_library_ready.clear()
            self._removal_runtime_invalidated = False
            # It committed: what was in force before it no longer matters.
            self._inventory_before_removal = None

    def _restore_runtime_after_uncommitted_removal(self) -> None:
        """Release a speculative barrier when no physical lifecycle committed."""
        with self._runtime_config_lock:
            if not self._removal_runtime_invalidated:
                return
            self._removal_runtime_invalidated = False
            before = self._inventory_before_removal
            self._inventory_before_removal = None
            valid, problem = (
                before if before is not None else (self._inventory_valid, self._inventory_problem)
            )
            # Nothing was deleted, so the inventory validity in force before
            # comes back: an inventory left invalid by an earlier removal whose
            # scans failed stays invalid, with its notice. "This changed
            # nothing" validates nothing. The scan lifecycle is not rewound: a
            # refresh that finished meanwhile stands as it ended, and one still
            # running ends on its own. The stamp still moves, so whatever was
            # captured before or meanwhile stays rejected.
            self._inventory_stamp += 1
            self._inventory_valid = valid
            self._inventory_problem = problem
        if valid:
            self._publish_runtime_for_context()
        else:
            # Still invalid: publish nothing new, but let a request captured during
            # the removal reach its terminal outcome (and release its hold).
            self._ensure_runtime_compile_queued()

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
        self._invalidate_inventory(f"{item.name} was removed; the library is being scanned again")
        failures = self._session.commit_removal(
            intent,
            artifacts_already_clean=artifacts_already_clean,
            artifact_source_root=artifact_source_root,
            artifact_lookup_root=artifact_lookup_root,
            artifact_lookup_parent=artifact_lookup_parent,
            artifact_source_context=artifact_source_context,
        )
        self._stills.forget(item.path)
        # Plain idle on purpose: only the headless --service mode gets here,
        # and it draws no frames to starve it.
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
        listing = playlist_store.worker_copy()
        schedule_store = self._session.schedules
        display_store = self._session.displays

        def work() -> _PlaylistDeleteResult:
            # The playlist is the authoritative lifecycle boundary.  Once it
            # commits, every independent cleanup is attempted and reported;
            # throwing on the first tail would hide a real deletion and leave
            # later references needlessly dangling.  The one tail checked
            # first is a saved default that could not then be cleared, because
            # the settings are read-only or can't be read at all: refusing
            # the whole deletion beats a default naming nothing.
            #
            # What names the playlist is never taken from what the window
            # remembers. Each reference is judged where it is read, under its
            # own store's lock, by the compiler's lookup against the
            # playlists as they are on disk now (`_playlist_namer`): its id,
            # or its name in any letter case. So a default another program
            # saved since the window opened is seen, refused if it can't be
            # cleared, and cleared if it can.
            names = _playlist_namer(listing.worker_copy(rebase=True), playlist.id)
            config.require_playlist_deletable(names)
            playlist_store.delete(playlist.id)
            failures: list[str] = []
            try:
                schedule_store.forget_playlist_where(names)
            except Exception as error:
                failures.append(f"schedule rules: {error}")
            try:
                display_store.forget_playlist_where(names)
            except Exception as error:
                failures.append(f"display assignments: {error}")
            saved_settings: config.Settings | None = None
            try:
                forget = functools.partial(config.forget_playlist_default, names)
                saved_settings = _settings_published(forget)
            except config.ConfigError as error:
                failures.append(f"saved default: {error}")
            return _PlaylistDeleteResult(
                playlist.id,
                playlist.name,
                settings=saved_settings,
                failures=tuple(failures),
            )

        return work

    def prepare_playlist_rename(
        self, reference: str, name: str
    ) -> Callable[[], PlaylistRenameResult]:
        """Rename one playlist at the head of the lane, keeping what names it by name working.

        An older profile's default, rule or display assignment may name the
        playlist by its name, which the compiler resolves (by id, then by
        name); renaming it would leave that naming nothing, and the compiler
        would build without it (`runtime_config.LeftOut`). So, in this one task:

        1. Read every store first, writing nothing: the playlists file (with
           the new name checked as the rename would check it), the schedule
           and display stores, and the settings, always: whether the default
           names the playlist is only known from the file. One that can't be
           read, is from a newer version, or would refuse the name refuses
           the whole rename before anything is written.
        2. Give each reference the playlist's id, one transaction per store:
           the default, every rule, every display assignment. The id names the
           same playlist before and after, so each is invisible on its own.
        3. Rename the playlist last: the one change anybody sees.

        After step 1 the only failures left are the stores' own (a lock, the
        disk). One after something was saved returns what was saved and why
        it stopped (`PlaylistRenameResult`), never "nothing changed"; the
        callers adopt and publish it (`adopt_playlist_rename`). No new field
        is stored.

        Which references name the playlist is never taken from what the window
        remembers. Each is judged where it is read, under its own store's
        lock, by the compiler's lookup against the playlists as they are on
        disk when the worker runs (`_playlist_namer`), so a default, rule or
        assignment another program saved by name since the window opened is
        given the id too.
        """
        playlist = self._session.playlists.find(reference)
        pid = playlist.id
        store = self._session.playlists
        listing = store.worker_copy()
        schedule_store = self._session.schedules
        display_store = self._session.displays

        def work() -> PlaylistRenameResult:
            for problem in (
                store.rename_problem(pid, name),
                schedule_store.durable_problem(),
                display_store.durable_problem(),
            ):
                if problem:
                    raise ValueError(problem)
            names = _playlist_namer(listing.worker_copy(rebase=True), pid)

            def by_name(saved_reference: str) -> bool:
                """It names the playlist, and not by the id that survives a rename."""
                return saved_reference != pid and names(saved_reference)

            config.require_playlist_renamable(by_name)
            saved: list[str] = []
            settings: config.Settings | None = None
            try:
                repoint = functools.partial(config.repoint_playlist_default, by_name, pid)
                settings = _settings_published(repoint)
                if settings is not None:
                    saved.append("the default playlist")
                rules = schedule_store.retarget_where(by_name, pid)
                if rules:
                    saved.append(f"{rules} schedule rule{'' if rules == 1 else 's'}")
                assigned = display_store.retarget_where(by_name, pid)
                if assigned:
                    saved.append(
                        f"{assigned} display\u2019s own playlist"
                        if assigned == 1
                        else f"{assigned} displays\u2019 own playlists"
                    )
                renamed = store.rename(pid, name)
            except Exception as error:
                if not saved:
                    raise  # nothing was saved: the plain refusal is true
                return PlaylistRenameResult(playlist, None, settings, tuple(saved), str(error))
            return PlaylistRenameResult(playlist, renamed, settings, tuple(saved))

        return work

    def adopt_playlist_rename(self, result: RenameResult) -> None:
        """Take a rename's saved default on GTK, and say what a partial rename saved.

        Both windows call this, then publish: what was saved reaches the
        runtime whether or not the rename itself did.
        """
        if result.renamed is None:
            self.window_report(rename_partly_saved(result))
        if result.settings is None:
            return
        previous = self._settings
        with self._settings_authoring_lock:
            pending = self._pending_settings_on(result.settings)
            self._settings_requested = replace(result.settings, **pending).validated()
        self._adopt_settings(previous, result.settings)

    def prepare_colors_forget(self, connector: str) -> Callable[[], ColorsForgotten]:
        """Forget Display's colors step: decided at the head of the lane, written on the worker.

        It stops the desktop colors following ``connector`` only while they
        still do and displays are mirrored, where that choice has no effect:
        never from when Forget was asked for. Here, on GTK after everything
        queued before it has saved, a newer choice still queued wins: another
        display for the colors, or independent displays (which need one;
        clearing it under them would make them mirrored again,
        `config.Settings.validated`). Otherwise what is requested no longer
        follows ``connector`` until the worker has run, so a choice made
        meanwhile is made against that. The worker decides again under the
        settings lock, against what ``settings.toml`` holds then
        (`config.forget_color_source`). `adopt_colors_forget` takes the result.
        """
        kept = "displays are now controlled separately, which needs a display for the colors"
        with self._settings_authoring_lock:
            pending = {
                key: value for key, (_gen, value) in self._settings_authoring_pending.items()
            }
            requested = self._settings_requested
            if "theme_source_connector" in pending:
                return lambda: ColorsForgotten()  # a newer colors choice: nothing to say
            if requested.display_mode != config.DISPLAY_MODE_MIRRORED:
                still = requested.theme_source_connector == connector
                return lambda: ColorsForgotten(kept=kept if still else "")
            if requested.theme_source_connector == connector:
                self._settings_requested = replace(requested, theme_source_connector="").validated()

        def clear() -> ColorsForgotten:
            try:
                held, wrote = config.forget_color_source(connector)
            except config.SettingsNotDurableError as error:
                # Published, then the folder sync failed: saved, and said so.
                state_file.report_not_durable(str(error))
                try:
                    return ColorsForgotten(config.load_strict())
                except config.ConfigError:
                    return ColorsForgotten(error.settings)
            if wrote:
                return ColorsForgotten(held)
            still = held.theme_source_connector == connector
            return ColorsForgotten(kept=kept if still else "")

        return clear

    def adopt_colors_forget(self, result: ColorsForgotten | None) -> None:
        """Take what Forget Display's colors step saved, on GTK; else what is requested again.

        Either way the requested settings are what is saved with every edit
        still queued applied, which undoes what `prepare_colors_forget`
        projected when the step wrote nothing.
        """
        saved = result.settings if result is not None else None
        previous = self._settings
        with self._settings_authoring_lock:
            base = saved if saved is not None else previous
            self._settings_requested = replace(base, **self._pending_settings_on(base)).validated()
        if saved is not None:
            self._adopt_settings(previous, saved)

    def _adopt_playlist_delete(self, result: _PlaylistDeleteResult) -> tuple[str, ...]:
        """Reconcile every portion which committed, even after partial cleanup."""
        failures = list(result.failures)
        if result.settings is not None:
            previous = self._settings
            with self._settings_authoring_lock:
                self._settings_requested = result.settings
            self._adopt_settings(previous, result.settings)
        # The app's own record of a pick isn't written for one made through the service
        # (ctl, the companion), so the service's status names it too. A pick the reply
        # can't name is not released on doubt: it may be another playlist's, which the
        # person still wants. The service itself drops a pick (the whole screen's or a
        # display's) whose playlist the published document no longer has, so this
        # release is for the one this app can name.
        manual, _displays = runtime_truth.pick_holders(
            runtime_truth.from_status(self.runtime_status),
            self._session.manual_playlist,
            result.playlist_id,
        )
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
        # Plain idle on purpose: a background rescan nothing waits on, kept
        # after the headless re-apply above.
        GLib.idle_add(self.refresh_library)

    def _reapply_current(self) -> bool:
        # Not once shutdown has begun: nothing is started on a process that is
        # ending, and the settle's loop must not wait for it (review r13c, G1-L1).
        if self._service_start and not self._shutting_down:
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

        # A control reply: above redraw, like every other one.
        GLib.idle_add(deliver, priority=COMPLETION_PRIORITY)  # type: ignore[call-arg]

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
        # playlist. A quiet minute costs the rules' time matching and one
        # comparison: the library is indexed only once a matching rule names
        # a playlist (`Session.compiling_schedule_rules`).
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

    def _pending_settings_on(self, base: config.Settings) -> dict[str, Any]:
        """The fields still queued, as they read on top of ``base``. Lock held by the caller.

        A queued folder gesture is re-applied to ``base``'s folders, so what
        the window shows meanwhile follows what was actually saved (a folder
        another program added included), and a gesture whose batch failed is
        not shown as though it were still coming.
        """
        pending = {key: value for key, (_gen, value) in self._settings_authoring_pending.items()}
        if self._settings_roots_edits:
            shown = config.edited_roots(
                base.roots, [edit for _gen, edit in self._settings_roots_edits]
            )
            newest = max(accepted for accepted, _edit in self._settings_roots_edits)
            self._settings_authoring_pending["roots"] = (newest, shown)
            pending["roots"] = shown
        return pending

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]:
        """Unrecognized settings.toml keys; non-empty means settings are read-only."""
        return self._settings_unknown_keys

    def edit_roots_async(
        self,
        edit: config.RootsEdit,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> bool:
        """Save one gesture on the library folders: add, remove, make first.

        The gesture is saved as the operation it is, applied on the worker to
        the folders ``settings.toml`` holds at that moment, and the dry run
        judges exactly that list. The window's snapshot only decides what it
        shows until then: a folder another program saved since stays.
        """
        return self._accept_settings({}, roots_edit=edit, on_success=on_success, on_error=on_error)

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

        Each field is a whole value, which is what a switch or a spin row
        means. The library folders are a list: a gesture on them goes through
        `edit_roots_async`. ``roots=`` here replaces the whole list.
        """
        return self._accept_settings(changes, on_success=on_success, on_error=on_error)

    def _accept_settings(
        self,
        changes: dict[str, Any],
        *,
        roots_edit: config.RootsEdit | None = None,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> bool:
        if not self.require_authoring_ready():
            if on_error is not None:
                on_error(self._migration_blocked_response().message)
            return False
        if self._settings_unknown_keys:
            # Refused before queuing anything, so no control ever shows a
            # value that was not saved. ``config.mutate`` refuses as well.
            message = config.read_only_message(self._settings_unknown_keys)
            if on_error is not None:
                on_error(message)
            else:
                self.window_report(f"Settings were not saved; nothing changed: {message}")
            return False
        try:
            with self._settings_authoring_lock:
                if roots_edit is None and "roots" in changes:
                    roots_edit = config.RootsEdit("set", roots=tuple(changes["roots"]))
                if roots_edit is not None:
                    # What the window shows until this saves; never what is written.
                    changes = {
                        **changes,
                        "roots": roots_edit.apply(self._settings_requested.roots),
                    }
                requested = replace(self._settings_requested, **changes).validated()
                self._settings_authoring_generation += 1
                generation = self._settings_authoring_generation
                self._settings_authoring_pending.update(
                    {key: (generation, value) for key, value in changes.items()}
                )
                if roots_edit is not None:
                    self._settings_roots_edits.append((generation, roots_edit))
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
            self._unprepared_settings_batch,
            self._finish_settings_batch,
            prepare=self._prepare_settings_batch,
            # A batch that did not save: its own generation goes back.
            failure=self._fail_settings_batch,
            # One that saved settles itself (`_finish_settings_batch`); this
            # only hears the lane if that ever raised regardless.
            saved_not_applied=self._settings_batch_saved_not_applied,
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

    @staticmethod
    def _unprepared_settings_batch() -> _SettingsResult:  # pragma: no cover - prepare replaces it
        raise RuntimeError("settings batch was not prepared")

    def _prepare_settings_batch(self) -> Callable[[], _SettingsResult]:
        """Take the coalesced batch at the head of the lane, and check it against what is saved.

        On GTK, after every write queued before it has saved: a default
        playlist that has nothing to play by now (its last wallpaper removed
        meanwhile, say) is refused here (`runtime_config.settings_refusal`),
        whichever page or window asked: it would be built as "All media", not
        as what was chosen. A change to the library folders or Wallpaper Engine scanning
        is checked on the worker, before ``settings.toml`` is written, by a dry
        run of the real scan and compiler (`wall_in_one.dry_run.sources`). A
        refusal fails the whole batch, as a failed write does: every control
        goes back to what is saved. Changes arriving after this snapshot stay
        pending and start their own batch.
        """
        with self._settings_authoring_lock:
            generation = self._settings_authoring_generation
            # What this batch owns, from here to its settlement, whether it
            # saves or not; anything accepted later is a later batch's.
            self._settings_batch_generation = generation
            changes = tuple(
                (key, field_generation, value)
                for key, (field_generation, value) in self._settings_authoring_pending.items()
            )
            self._settings_batch_turns_web_off = any(
                key == "allow_web_wallpapers" and not value for key, _gen, value in changes
            )
            roots_edits = tuple(edit for _gen, edit in self._settings_roots_edits)
        change_map = {key: value for key, _gen, value in changes}
        if change_map.get("active_playlist"):
            # A default is judged by the library: never by one the app knows is stale.
            self.require_current_library()
        refusal = runtime_config.settings_refusal(change_map, self._session)
        if refusal:
            # Its kind keeps the reply a sentence (`control.server.failed`).
            raise runtime_config.PlaybackGuardError(refusal)
        # The early answer, from what this process holds. The one that counts
        # is asked on the worker, of the playlists as they are saved then.
        default_refusal = (
            runtime_config.default_refusal_on_what_is_saved(change_map, self._session)
            if change_map.get("active_playlist")
            else None
        )
        sources = (
            self._sources_check()
            if any(key in {"roots", "scan_workshop"} for key, _gen, _value in changes)
            else None
        )
        return lambda: self._persist_settings_batch(
            generation, changes, sources, roots_edits, default_refusal
        )

    def _sources_check(self) -> Callable[[config.Settings, config.Settings], None]:
        """A worker-side dry run of new library sources (`dry_run.sources`), refusing by raising.

        Captured on GTK: the session's scanner and how to read what is saved
        (`Session.saved_view`). The worker takes the library, the stores and
        what uses each playlist from that view, as their files read then, and
        scans.
        """
        session = self._session
        scanner = session.scanner
        saved = session.saved_view()

        def check(before: config.Settings, after: config.Settings) -> None:
            if before.roots != after.roots:
                what = "that change to the library folders"
            elif before.scan_workshop != after.scan_workshop:
                what = (
                    "turning on Wallpaper Engine scanning"
                    if after.scan_workshop
                    else "turning off Wallpaper Engine scanning"
                )
            else:
                return
            try:
                view = saved()
                try:
                    verdict = dry_run.sources(
                        before,
                        after,
                        library=view.library,
                        pairing_store=view.pairings,
                        playlist_store=view.playlists,
                        favourite_store=view.favourites,
                        uses=dry_run.Uses.of(view),
                        scanner=scanner,
                    )
                finally:
                    view.shutdown()
            except config.SettingsUnreadableError:
                # A file the question turns on could not be read: said as
                # that, once, with its own kind (`runtime_config.unreadable`).
                raise
            except Exception as error:
                raise dry_run.CheckFailed(
                    f"what {what} would leave could not be checked: {error}"
                ) from error
            if verdict.refusals:
                raise runtime_config.PlaybackGuardError(
                    f"{what} would break playback: {runtime_config.listed(verdict.refusals)}"
                )

        return check

    def _persist_settings_batch(
        self,
        generation: int,
        changes: tuple[tuple[str, int, Any], ...],
        sources: Callable[[config.Settings, config.Settings], None] | None = None,
        roots_edits: tuple[config.RootsEdit, ...] = (),
        default_refusal: Callable[[], str] | None = None,
    ) -> _SettingsResult:
        def persist() -> _SettingsResult:
            if not changes:  # pragma: no cover - guarded by the GTK scheduler
                return _SettingsResult(generation, config.load_strict(), ())
            change_map = {key: value for key, _field_generation, value in changes}
            if default_refusal is not None:
                refused = default_refusal()
                if refused:
                    raise runtime_config.PlaybackGuardError(refused)
            wanted_default = change_map.get("active_playlist")
            if (
                isinstance(wanted_default, str)
                and wanted_default
                and playlists.Store.open().get(wanted_default) is None
            ):
                raise config.ConfigError(
                    "the chosen default playlist was deleted before Settings could save"
                )
            try:
                saved = (
                    config.update(change_map)
                    if sources is None
                    else _save_checked_sources(change_map, roots_edits, sources)
                )
            except config.SettingsNotDurableError as error:
                # Published, then the folder sync failed: this is no refusal.
                # Adopt what the file holds now, not the rejected-looking old
                # snapshot, and say the save happened.
                try:
                    published = config.load_strict()
                except config.ConfigError:
                    published = error.settings
                return _SettingsResult(generation, published, changes, str(error))
            return _SettingsResult(generation, saved, changes)

        # The barrier is for the folders and Workshop scanning. A move of the
        # settings that decide `Settings.preview_stills` (web wallpapers on or off,
        # the previews sub-option) is not one: turning web wallpapers off must always
        # land, and a library resolved under the other policy is refused at the
        # publication's admission instead (`Settings.covers`).
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
        """Settle the batch that saved: its own generation, exactly once, then its tail.

        Committed-but-not-adopted is this batch's to settle, never a failure:
        `_fail_settings_batch` is for a batch that did not save. The
        generation's callbacks are taken first and settled here whatever
        adopting the saved settings does (`_settle_saved_settings`); a newer
        generation accepted during the save keeps its fields and callbacks and
        saves as its own batch, started last.
        """
        callbacks, pending = self._take_saved_generation(result)
        previous = self._settings
        scans = self._library_scan_generation
        try:
            unapplied: Exception | None = None
            try:
                self._adopt_settings(previous, result.settings)
            except Exception as error:
                unapplied = error
            scanned = self._library_scan_generation != scans
            self._settle_saved_settings(previous, result, callbacks, unapplied, scanned=scanned)
        finally:
            if pending:
                self._start_settings_authoring()

    def _take_saved_generation(
        self, result: _SettingsResult
    ) -> tuple[
        list[
            tuple[
                int,
                Callable[[config.Settings], None] | None,
                Callable[[str], None] | None,
            ]
        ],
        bool,
    ]:
        """This batch's callbacks and fields, out of the shared lists; whether a tail remains."""
        with self._settings_authoring_lock:
            self._settings_batch_generation = None
            for key, field_generation, _value in result.changes:
                pending_field = self._settings_authoring_pending.get(key)
                if pending_field is not None and pending_field[0] <= field_generation:
                    self._settings_authoring_pending.pop(key, None)
            self._settings_roots_edits = [
                (accepted, edit)
                for accepted, edit in self._settings_roots_edits
                if accepted > result.generation
            ]
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
            newer = self._pending_settings_on(result.settings)
            try:
                self._settings_requested = replace(result.settings, **newer).validated()
            except TypeError, ValueError:
                # The newer fields don't combine with what was saved: they are
                # checked again when their own batch saves.
                self._settings_requested = result.settings
        return callbacks, pending

    def _settle_saved_settings(
        self,
        previous: config.Settings,
        result: _SettingsResult,
        callbacks: list[
            tuple[
                int,
                Callable[[config.Settings], None] | None,
                Callable[[str], None] | None,
            ]
        ],
        unapplied: Exception | None,
        *,
        scanned: bool = True,
    ) -> None:
        """Tell this generation's callers it saved, once each, and say what couldn't follow.

        They saved whatever adopting it did: each hears success (its value is
        what ``settings.toml`` holds), never its failure path, which says
        "nothing changed" and undoes intents. If adopting failed partway, the
        app still holds the saved settings (`_adopt_settings` takes them
        first); what they change is published and shown here, and that they
        couldn't be applied is said once. New library folders the window
        never got to scan (``scanned`` False) are scanned now; a scan that ran
        and failed is not repeated here: the library stays what it was, and
        is said not to be current, until a scan succeeds (Z-2).
        """
        if unapplied is not None:
            sources_changed = result.settings.library_sources != previous.library_sources
            if sources_changed and not scanned and not self._service_start:
                # Adoption stopped before the window's scan of the new folders
                # began. (The service scans inside adoption, `Session.update_settings`.)
                # Started before publishing, so the publication waits for it
                # rather than saying the library doesn't match the folders.
                try:
                    self.refresh_library()
                except Exception as error:  # said with the rest, below
                    unapplied = RuntimeError(f"{unapplied}; and the library scan: {error}")
        # Settled first: every caller hears it saved before anything here that
        # reaches the window, and one caller's failure is never another's.
        followed: list[str] = []
        for _generation, success, _failure in callbacks:
            if success is not None:
                try:
                    success(result.settings)
                except Exception as error:
                    followed.append(f"Settings were saved, but what follows them failed: {error}")
        if unapplied is not None:
            self._show_what_was_saved()  # never raises
            followed.append(f"Settings were saved, but they couldn't be applied: {unapplied}")
        if result.not_durable:
            followed.append(
                "Settings were saved, but Wall-in-One couldn't confirm they're safely on "
                f"disk: {result.not_durable}"
            )
        for said in followed:
            self.window_report(said)

    def _settings_batch_saved_not_applied(self, message: str) -> None:
        """The lane's saved-but-not-applied route for a settings batch: touch no generation.

        `_finish_settings_batch` takes and settles its own generation and
        starts any tail itself, so this normally follows a failure after all
        that; the lane says the message (`with_task_durability`). Above all it
        is not `_fail_settings_batch`, which would drop a newer accepted edit.
        Should the generation not have been taken, it is settled from what
        ``settings.toml`` holds, so the batch never wedges.
        """
        with self._settings_authoring_lock:
            took = self._settings_batch_generation
            changes = tuple(
                (key, field_generation, value)
                for key, (field_generation, value) in self._settings_authoring_pending.items()
                if took is not None and field_generation <= took
            )
        if took is None:
            return
        try:
            saved = config.load_strict()
        except config.ConfigError:
            saved = self._settings
        self._finish_settings_batch(_SettingsResult(took, saved, changes))

    def _fail_settings_batch(self, message: str) -> None:
        """Put the batch that did not save back to what is saved; nothing newer.

        Pre-commit only: a refusal before the write, or a write that failed
        (`saved_not_applied` takes the committed case). The batch at the lane
        head owns the generation it took (`_settings_batch_generation`); its
        fields and callbacks go back and fail. A generation accepted after it
        stays pending and starts its own batch. Refused before the lane head
        (shutting down, say), the batch took nothing: everything accepted so
        far fails, and nothing is retried.
        """
        with self._settings_authoring_lock:
            took = self._settings_batch_generation
            self._settings_batch_generation = None
            failed = took if took is not None else self._settings_authoring_generation
            callbacks = [
                callback for callback in self._settings_authoring_callbacks if callback[0] <= failed
            ]
            self._settings_authoring_callbacks = [
                callback for callback in self._settings_authoring_callbacks if callback[0] > failed
            ]
            for key in [
                key
                for key, (field_generation, _value) in self._settings_authoring_pending.items()
                if field_generation <= failed
            ]:
                self._settings_authoring_pending.pop(key)
            self._settings_roots_edits = [
                (accepted, edit)
                for accepted, edit in self._settings_roots_edits
                if accepted > failed
            ]
            # Before the lane head ``failed`` is the newest generation, so
            # nothing is left; after it, anything left is newer and retried.
            retry = bool(self._settings_authoring_pending or self._settings_authoring_callbacks)
            self._settings_authoring_running = retry
            self._settings_requested = replace(
                self._settings, **self._pending_settings_on(self._settings)
            ).validated()
            requested = self._settings_requested
        try:
            # Each caller settles first, before what reaches the window; one
            # caller's failure never skips another's.
            delivered = False
            raised: list[Exception] = []
            for _generation, _success, failure in callbacks:
                if failure is not None:
                    delivered = True
                    try:
                        failure(message)
                    except Exception as error:
                        raised.append(error)
            if self._window is not None:
                self._window.apply_settings(requested)
            if not delivered:
                self.window_report(f"Settings were not saved; nothing changed: {message}")
            if raised:
                raise raised[0]
        finally:
            if retry:
                self._start_settings_authoring()

    def update_settings(self, **changes: Any) -> config.Settings:
        """Save a settings change now, for the headless service and non-GUI callers.

        The same checks as the window's batched writes: a default playlist with
        nothing to play (`runtime_config.settings_refusal`), and new library
        sources by a dry run (`dry_run.sources`), before anything is written.
        """
        if self._settings_unknown_keys:
            raise config.SettingsReadOnlyError(self._settings_unknown_keys)
        if changes.get("active_playlist"):
            self.require_current_library()  # judged by the library, as in the window
        refusal = runtime_config.settings_refusal(changes, self._session)
        if not refusal and changes.get("active_playlist"):
            # And of the playlists as they are saved now, read here.
            refusal = runtime_config.default_refusal_on_what_is_saved(changes, self._session)()
        if refusal:
            raise config.ConfigError(refusal)
        write: Callable[[], config.Settings] = lambda: config.update(changes)  # noqa: E731
        if "roots" in changes or "scan_workshop" in changes:
            # The same dry run as the batch's, synchronous like everything
            # here, and on the same terms: judged against what is saved now,
            # and written only if that is still what is saved.
            check = self._sources_check()
            whole = (
                (config.RootsEdit("set", roots=tuple(changes["roots"])),)
                if "roots" in changes
                else ()
            )
            write = lambda: _save_checked_sources(changes, whole, check)  # noqa: E731
        previous = self._settings
        # Explicit legacy-service and non-GUI callers retain a synchronous
        # boundary.  They still rebase semantic fields so a stale process
        # cannot erase unrelated settings authored elsewhere.
        candidate = (
            self.legacy_authoring_sync(lambda: _settings_published(write))
            if self.legacy_service
            else _settings_published(write)
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
        library_sources_changed = (
            candidate.roots,
            candidate.scan_workshop,
            candidate.preview_stills,
        ) != (
            previous.roots,
            previous.scan_workshop,
            previous.preview_stills,
        )
        async_scan = library_sources_changed and not self._service_start
        # ``--service`` rescans synchronously, here.
        rescan_now = library_sources_changed and not async_scan
        web_turned_off = previous.allow_web_wallpapers and not candidate.allow_web_wallpapers
        # Persist before adoption. A failed write must leave the application,
        # Session, runtime document and visible controls on the same last-known
        # durable snapshot.
        self._settings = candidate
        if candidate.allow_web_wallpapers:
            with self._runtime_config_lock:
                self._runtime_web_off_owed = 0
        self._session.update_settings(candidate, rescan_library=rescan_now and not web_turned_off)
        if web_turned_off:
            # Web wallpapers going off reach the service first, whatever else this
            # save changed and whatever a scan then does (owner contract since 0.4.0):
            # before the synchronous rescan, and never held by one in flight.
            self._publish_web_off()
            if rescan_now:
                self._session.refresh()
        if rescan_now:
            self._session.sync_with_noctalia()
        # A save that scans nothing on the side publishes now (web off already has,
        # unless a rescan followed). Otherwise, do not publish a temporary document
        # which combines new roots with an old library: the completed async scan
        # publishes the coherent pair.
        if not async_scan and (rescan_now or not web_turned_off):
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
        web_turned_on = candidate.allow_web_wallpapers and not previous.allow_web_wallpapers
        if async_scan:
            self.refresh_library()
        elif library_sources_changed or web_turned_on:
            # Web wallpapers got no stills while they were off: theirs are made
            # now, and the rescan after them compiles them in.
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
            requested = replace(updated, **self._app._pending_settings_on(updated)).validated()
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
            lambda: _settings_published(work),
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
            lambda: _settings_published(work),
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
            lambda: _settings_published(lambda: config.update({"cycle_interval": seconds})),
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
            lambda: _settings_published(work),
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
        # ``is False``, not falsiness: lightweight Application doubles predate
        # the answer and return None, which still means the page opened.
        if self._app.present_page(page) is False:
            return Response.failure(
                f"the {page} page is not available in the new UI yet; the window is open. "
                "To use it, choose the classic interface in Settings and restart "
                "Wall-in-One, or start it with --ui=classic.",
                kind="not-in-new-ui",
            )
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
            self._app.require_current_library()
            session = self._app.session
            item = server.resolve(session.library, value, verb="select")
            health = session.pairings.health(pairings.Identity.of(item))
            if health.is_borked:
                raise ValueError(
                    f"Playback unavailable for {item.name}; "
                    "see Library for details and removal options"
                )
            runtime_config.refuse_unresolved_quick_choice(item, session)
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
                stale = self._app.pairing_currency_refusal(item)
                if stale:
                    return server.failed(
                        runtime_config.LibraryNotCurrentError(
                            f"{item.name}\u2019s still was not changed: {stale}"
                        )
                    )
            elif item.is_moving and item.paired_still is None:
                # The headless service resolves the default from its scan.
                stale = self._app.pairing_currency_refusal(item)
                if stale:
                    return server.failed(
                        runtime_config.LibraryNotCurrentError(
                            f"{item.name}\u2019s still was not changed: {stale}"
                        )
                    )
                # Of what is saved now, read here: this service is its own worker.
                unresolved = runtime_config.unresolved_on_what_is_saved(item, session)()
                if unresolved:
                    return server.failed(
                        runtime_config.PlaybackGuardError(
                            f"{item.name}\u2019s still was not changed: it would have none, "
                            f"and {runtime_config.listed(unresolved)}"
                        )
                    )
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
            self._legacy_authoring(
                lambda: session.pairings.choose_palette(item, policy, keep_mode=True)
            )
            self._app.pairing_changed(item)
            return Response.success(f"{item.name} asks for {policy.encode()}")

        def prepare() -> Callable[[], tuple[MediaItem, pairings.Pairing]]:
            session = self._app.session
            item = server.resolve(session.library, source, verb="palette")
            pairing_store = session.pairings

            def work() -> tuple[MediaItem, pairings.Pairing]:
                return item, pairing_store.choose_palette(item, policy, keep_mode=True)

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
            if item.is_moving and item.paired_still is None:
                # The headless service resolves the default from its scan.
                stale = self._app.pairing_currency_refusal(item)
                if stale:
                    return server.failed(
                        runtime_config.LibraryNotCurrentError(f"{item.name} was not reset: {stale}")
                    )
                unresolved = runtime_config.unresolved_on_what_is_saved(item, session)()
                if unresolved:
                    return server.failed(
                        runtime_config.PlaybackGuardError(
                            f"{item.name} was not reset: it would have no still, and "
                            f"{runtime_config.listed(unresolved)}"
                        )
                    )
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
        """`displays` -- ask the service for the screens away from GTK, answer later.

        GTK's own monitors are read here, on the main thread, for when no
        service answers (see `wallpaper.outputs.discover`).
        """
        session = self._app.session
        # Assignments store the playlist *id*, so a rename does not break them.
        # Nobody reads ids, so they are resolved back to names here rather than
        # printed raw. Capture only immutable authoring values; the worker must
        # never receive the live Session.
        names = tuple((playlist.id, playlist.name) for playlist in session.playlists.all())
        assignments = session.displays.all()
        fallback = monitors.snapshot()

        def work() -> Response:
            attached = outputs.discover(fallback)
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
        what = f"{connector}\u2019s playlist was not changed"
        if self._app.legacy_service:
            session = self._app.session
            playlist = session.playlists.find(wanted.strip())
            self._app.require_current_library()
            assign = runtime_config.checked_playlist_use(
                playlist.id,
                what,
                lambda: session.displays.assign(connector, playlist.id),
                session,
            )
            self._legacy_authoring(assign)
            self._app.runtime_config_changed()
            return Response.success(f"{connector} shows {playlist.name}")

        def prepare() -> Callable[[], playlists.Playlist]:
            # Whether it can be put into use is judged by the library: never
            # by one the app knows is stale.
            self._app.require_current_library()
            session = self._app.session
            playlist = session.playlists.find(wanted.strip())
            display_store = session.displays

            def work() -> playlists.Playlist:
                display_store.assign(connector, playlist.id)
                return playlist

            return runtime_config.checked_playlist_use(playlist.id, what, work, self._app.session)

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
            self._app.require_current_library()
            add = runtime_config.checked_entries_addition(
                playlist.id,
                [item.path],
                lambda: session.playlists.add(playlist.id, item.path),
                session,
            )
            self._legacy_authoring(add)
            self._app.playlists_changed()
            return Response.success(f"{item.name} added to {playlist.name}")

        def prepare() -> Callable[[], tuple[playlists.Playlist, MediaItem]]:
            # Never from a library the app knows is stale: a removed file it
            # still lists would leave this playlist holding nothing the
            # compiler keeps once the scan lands.
            self._app.require_current_library()
            session = self._app.session
            playlist = session.playlists.find(name)
            item = self._app.current_item_for_authoring(
                server.resolve(session.library, source, verb="playlist-add")
            )
            playlist_store = session.playlists

            def work() -> tuple[playlists.Playlist, MediaItem]:
                playlist_store.add(playlist.id, item.path)
                return playlist, item

            return runtime_config.checked_entries_addition(playlist.id, [item.path], work, session)

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
            self._app.require_current_library()
            remove = runtime_config.checked_entries_removal(
                playlist.id,
                [entry],
                lambda: session.playlists.remove_entry(playlist.id, entry),
                session,
            )
            self._legacy_authoring(remove)
            self._app.playlists_changed()
            return Response.success(f"removed {entry} from {playlist.name}")

        def prepare() -> Callable[[], playlists.Playlist]:
            # Judged by the library: never by one the app knows is stale.
            self._app.require_current_library()
            session = self._app.session
            playlist = session.playlists.find(name)
            playlist_store = session.playlists

            def work() -> playlists.Playlist:
                playlist_store.remove_entry(playlist.id, entry)
                return playlist

            return runtime_config.checked_entries_removal(playlist.id, [entry], work, session)

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
        kept = {rule.id for rule in session.compiling_schedule_rules()}
        return Response.success(
            schedules.describe(
                session.schedules.rules,
                session.settings.active_playlist,
                datetime.now(),
                {one.id: one.name for one in session.playlists.all()},
                omitted={rule.id for rule in session.schedules.rules} - kept,
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

        what = "The schedule rule was not added"
        if self._app.legacy_service:
            playlist = self._app.session.playlists.find(name)
            self._app.require_current_library()
            add = runtime_config.checked_playlist_use(
                playlist.id, what, lambda: work_for(playlist), self._app.session
            )
            rule = self._legacy_authoring(add)
            return finish((playlist, rule))

        def prepare() -> Callable[[], tuple[playlists.Playlist, schedules.Rule]]:
            # Judged by the library: never by one the app knows is stale.
            self._app.require_current_library()
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

            return runtime_config.checked_playlist_use(playlist.id, what, work, self._app.session)

        def unprepared() -> tuple[playlists.Playlist, schedules.Rule]:  # pragma: no cover
            raise RuntimeError("schedule rule was not prepared")

        return self._app.authoring_off_thread(unprepared, finish, prepare=prepare)

    def drop_schedule_rule(self, value: str | None) -> server.Outcome:
        session = self._app.session
        rule_id = (value or "").strip()

        def finish(changed: bool) -> Response:
            if not changed:
                # Nothing was written, so the lane answers this as the plain
                # refusal it is (`_after_failed_finish`), never "saved".
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

    def name_schedule_rule(self, value: str | None) -> server.Outcome:
        """Name a rule by its id, or clear its name when none is given.

        The first name moves schedules.json to version 3; see
        `schedules.Store.set_name`.
        """
        session = self._app.session
        rule_id, name = server.parse_rule_name(value)

        def finish(rule: schedules.Rule) -> Response:
            self._app.schedule_edited()
            if rule.name is None:
                return Response.success(f"rule {rule.id} has no name")
            return Response.success(f"rule {rule.id} is called {rule.name}")

        if self._app.legacy_service:
            return finish(self._legacy_authoring(lambda: session.schedules.set_name(rule_id, name)))
        schedule_store = session.schedules
        return self._app.authoring_off_thread(
            lambda: schedule_store.set_name(rule_id, name),
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
                # Before the journal intent and the physical step, as a
                # RemovalPlan does on its worker.
                blocked = session.removal_cleanup_blocker(item)
                if blocked is not None:
                    return Response.failure(blocked[1], kind=blocked[0])
                # The source's lifecycle locks from before the dry run to after
                # the commit, as `RemovalPlan.run` holds them (it explains why).
                held = contextlib.ExitStack()
                try:
                    held.enter_context(stills.held_for_removal(item))
                except OSError as error:
                    held.close()
                    kind, message = removal_lock_refusal(item, error)
                    return Response.failure(message, kind=kind)
                with held:
                    return remove_held(item)

            def remove_held(item: MediaItem) -> Response:
                try:
                    # The same dry run, synchronous like everything here.
                    session.check_removal(item)
                except ValueError as error:
                    return server.failed(error)
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
            # Checked on the worker by the plan's own dry run (`RemovalPlan.run`).
            plan = self._app.session.prepare_removal_plan(item, trash=not item.deletable)
            self._app._invalidate_runtime_for_pending_removal()
            self._app._removal_active = True
            return self._app._removal_run(plan)

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
            # scan ends in the grid. Plain idle on purpose: nothing waits on
            # this rescan, and the reply goes back on its own.
            GLib.idle_add(self._app.refresh_library)
            return Response.success(f"{done.describe()} -> {done.result.path}")

        return self._app.browse_off_thread(work)

    def quit(self) -> Response:
        if not self._app.legacy_service:
            return Response.failure(
                "the GUI is open, but the wallpaper runtime is not running",
                kind="runtime-not-running",
            )
        # Plain idle on purpose: only the headless --service mode gets here.
        GLib.idle_add(self._app.request_quit)
        return Response.success("quitting")


def run(
    argv: list[str] | None = None,
    *,
    service: bool = False,
    initial_page: str | None = None,
    ui: UiKind | None = None,
    preferred_ui: UiKind | None = None,
) -> int:
    """Start, or hand off to, the single Wall-in-One instance.

    ``ui`` is None unless ``--ui`` was given. Without it the window is
    ``preferred_ui``, the interface chosen in Settings, or `DEFAULT_UI` (the
    new interface). Either
    choice belongs to the process that starts: a launch which finds an
    instance already running presents that instance's window unchanged, and
    notes that only for an explicit ``--ui``.
    """
    # GtkApplication overwrites prgname with the application id on Wayland, so
    # setting it here would be cosmetic at best and misleading at worst. The
    # Wayland app-id comes from paths.APPLICATION_ID; see
    # docs/compositor-window-rules.md.
    GLib.set_application_name("Wall-in-One")
    application = Application(
        service=service,
        initial_page=initial_page,
        ui=ui if ui is not None else preferred_ui if preferred_ui is not None else DEFAULT_UI,
    )
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
        if ui is not None:
            # The activation forwarded below carries only the package and the
            # page: older builds define its (ss) shape, so it cannot grow.
            print(
                f"note: Wall-in-One is already running, so --ui={ui} was not applied; "
                "it keeps the interface it started with. Close it and start again to switch.",
                file=sys.stderr,
            )
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
