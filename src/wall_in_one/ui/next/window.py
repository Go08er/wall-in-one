"""The new interface's window: the redesign's shell over the application's real data.

`NextWindow` is the prototype's `ShellWindow` -- sidebar, header, banners,
player bar, frosted backdrop -- over a `RealAppState`, with every page real:
the Library and its inspector, which edit through the application's
authoring lane, the Store, Playlists, Schedule, Displays, and Settings
(`settings.SettingsPage`, with the palette manager), which also offers the
interface to start, so the new one can be left from inside. ``ctl open
schedules``, and its alias ``displays``, open Schedule. A page missing from
`PORTED` would still show its "not in the new interface yet" placeholder and
make `show_page` answer False (``ctl open``'s ``not-in-new-ui``); none is
now.

It implements every `WindowServices` member. Runtime status reaches it
through the adapter's own `RuntimeStatusModel` subscription, so the forwarded
``show_runtime_*`` calls only say once, in a toast, what the classic window
toasts: a renderer that stopped, degraded display discovery, an invalid status
reply. Rescan is in the main menu as well as on F5, as classic has it in the
header. Library, playlist and health changes arrive as the application hands
over its `Session`. Pictures come from the app's thumbnail pipeline
(`LibraryThumbnails`). The window style and its dials come from ``ui.toml``
and are rendered by the application into its own stylesheet
(`Application.set_window_glass`).

**The log.** Settings ends with this session's log (`log_section.LogSection`):
what the application told the user and what the runtime's status said as it
changed, kept in memory by the application (`activity_log.ActivityLog`), with
file paths hidden unless the user shows them. The classic window has no log.

**After an update** the classic window's one-time Tidy up card sits above every
page (`TidyCard`); its Review opens Settings at Tidy up, whose actions -- the
palette template fix in Noctalia's settings among them -- run only when the
user presses Apply.

**First run.** The window asks the application's first-run questions on its
own welcome screen (`welcome.Welcome`) instead of in dialogs: it is a
`FirstRunHost`. While it covers the window, every window action but the
shortcuts help is off (`ShellWindow.block_actions`), so its keys and menu
items change nothing underneath, as under a classic modal dialog. The
questions, their words and what each answer does are the application's,
shared with the classic window. A fresh profile opens on the
welcome screen; otherwise it appears only for a question, or for the
older-data check if that runs long. A question put off ("Not now"), and changes
paused by the authoring gate, leave a notice under the header whose button asks
again (`Application.reconsider_older_data`, `ask_for_library_folder`,
`retry_authoring_repair`).

Teardown happens on ``unrealize``: GTK 4's ``destroy()`` unrealizes at once but
emits "destroy" only at dispose, which the status model's reference would
postpone forever. The window then leaves the model, saves a preference
change that is still waiting, and stops its thumbnail workers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk

from wall_in_one import __version__, config
from wall_in_one.session import Session
from wall_in_one.theme import source
from wall_in_one.ui.launch_rows import LaunchRows
from wall_in_one.ui.next import displays, library, playlists, schedule, status_line, store, thumbs
from wall_in_one.ui.next.catalog import CLASSIC_HINT
from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
from wall_in_one.ui.next.log_section import LogSection
from wall_in_one.ui.next.page import Placeholder
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_state import RealAppState
from wall_in_one.ui.next.settings import SettingsPage
from wall_in_one.ui.next.shell import ShellWindow
from wall_in_one.ui.next.state import AppState, Banner
from wall_in_one.ui.next.welcome import Welcome, is_progress
from wall_in_one.ui.plain_text import plain_toast
from wall_in_one.ui.setup_section import SetupCard, SetupSection
from wall_in_one.ui.tidy_section import TidyCard, TidySection

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: `show_page` names (as ``ctl open`` and ``--open-page`` give them) -> shell pages.
_PAGES: Final = {
    "media": "library",
    "browse": "store",
    "playlists": "playlist",
    "schedules": "schedule",
    "displays": "displays",
    "settings": "settings",
}
#: The pages this interface has: every one, now. One left out would be a placeholder.
PORTED: Final = frozenset({"media", "browse", "playlists", "settings", "schedules", "displays"})
#: Kept for callers of the placeholder era.
NOT_PORTED_HINT: Final = CLASSIC_HINT
#: How long the older-data check may run before the welcome screen says so.
#: It runs at every start and usually takes a moment; the classic window shows
#: its progress dialog at once.
CHECK_GRACE_MS: Final = 1000

#: The window's own notices (`_window_banner`) and their buttons' actions.
#: The library is loaded only once the older data has its answer (an import
#: may bring other folders), so these say that too.
OLDER_DATA_WAITING: Final = (
    "Settings and playlists from the older Noctalia plugin are waiting for your choice. "
    "Until then, the library isn\u2019t loaded and changes can\u2019t be saved."
)
OLDER_DATA_UNCHECKED: Final = (
    "Wall-in-One couldn\u2019t check for settings from the older Noctalia plugin. "
    "Until it can, the library isn\u2019t loaded and changes can\u2019t be saved."
)
REPAIR_NEEDED: Final = (
    "Wall-in-One couldn\u2019t repair references to playlists that are gone, so the "
    "library isn\u2019t loaded and changes can\u2019t be saved."
)
NOT_OPENED: Final = (
    "Wall-in-One couldn\u2019t open this profile for changes, so the library isn\u2019t "
    "loaded and nothing can be saved:"
)
NO_FOLDER: Final = (
    "There\u2019s no wallpaper folder yet, so the library is empty and nothing is downloaded."
)
PUBLICATION_FAILED: Final = (
    "Changes aren\u2019t reaching playback, which keeps the last setup that worked:"
)
OLDER_DATA: Final = "older-data"
REPAIR: Final = "repair"
LIBRARY_FOLDER: Final = "library-folder"
PLAYLISTS: Final = "playlists"


def settings_page(state: AppState, keeper: UiPrefsKeeper, application: Application) -> SettingsPage:
    """Settings over the application: its rows, the next start's interface, Tidy up, the log.

    The interface and GPU rows (`LaunchRows`) write ui.toml through the
    window's keeper, like every window preference here, show what was saved,
    and are off while ui.toml cannot be written. Tidy up is the classic
    section itself, on the application's own lane. The log is the
    application's session log (`Application.activity_log`); what Copy says
    is a toast, not another log entry.
    """
    return SettingsPage(
        state,
        launch=LaunchRows(keeper, application.window_report),
        tidy=TidySection(application),
        setup=SetupSection(application),
        log=LogSection(application.activity_log, state.toast),
    )


class NextWindow(ShellWindow):
    """The redesigned window over real data; implements `WindowServices`."""

    def __init__(self, application: Application, settings: config.Settings) -> None:
        self._settings = settings
        self._application = application
        self.welcome = Welcome(self._answer)
        self._root: Gtk.Stack | None = None
        self._first_run_idle = 0
        self._check_grace = 0
        #: What was last said about the runtime, so each is said once (as classic).
        self._runtime_error = ""
        self._protocol_error = ""
        self._thumbnails: LibraryThumbnails | None = LibraryThumbnails()
        thumbs.install(self._thumbnails)
        self._prefs = UiPrefsKeeper(application.window_report)
        self._state = RealAppState(application, self._prefs, authoring=application)
        application.set_window_glass(self._state.glass())
        super().__init__(
            application,
            self._state,
            {
                "library": library.create,
                "store": store.create,
                "playlist": playlists.create,
                "schedule": schedule.create,
                "displays": displays.create,
                "settings": lambda state: settings_page(state, self._prefs, application),
            },
            version=__version__,
        )
        # Offered once after an update, above every page, as in the classic
        # window: it plans when the window first appears and writes ui.toml
        # only when dismissed. Review opens Settings at Tidy up.
        self.tidy_card = TidyCard(application, self.open_tidy)
        self.content_view.add_top_bar(self.tidy_card)
        self.connect("map", lambda _window: self.tidy_card.start())
        # While something it needs is missing, the same card as the classic
        # window's (docs/missing-components.md). Details opens Settings at Setup.
        self.setup_card = SetupCard(application, self.open_setup)
        self.content_view.add_top_bar(self.setup_card)
        self.connect("map", lambda _window: self.setup_card.start())
        self._state.connect("changed", self._on_state_changed)
        self._show_status()
        self.connect("realize", lambda _window: self._resume())
        self.connect("unrealize", lambda _window: self._teardown())
        if (
            application.get_is_registered()
            and not application.first_activation_done
            and not settings.roots
        ):
            # The running app's first window, on a fresh profile: its first
            # question follows at once, so open on the welcome screen rather
            # than flash the empty window first. (A window built outside a
            # running app, as tests do, waits for a question like any other.)
            self._reveal_welcome()
        # After the activation that builds this window has asked what it asks.
        self.first_run_changed()

    def extra_menu_sections(self) -> Sequence[tuple[str | None, Gio.Menu]]:
        """Rescan, which classic has as a header button; F5 does the same."""
        if self._state.library_folders is None:
            return ()
        library = Gio.Menu()
        library.append("Rescan the Library", "win.refresh")
        return [(None, library)]

    def wrap_body(self, body: Gtk.Widget) -> Gtk.Widget:
        """The window's body, with the welcome screen beside it."""
        root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        root.add_named(body, "main")
        root.add_named(self.welcome.widget, "welcome")
        root.set_visible_child_name("main")
        self._root = root
        return root

    # -- lifetime -------------------------------------------------------------
    def _resume(self) -> None:
        if self._thumbnails is None:
            self._thumbnails = LibraryThumbnails()
            thumbs.install(self._thumbnails)
        self._prefs.reopen()
        self._state.listen()
        settings = self.pages.get("settings")
        if isinstance(settings, SettingsPage) and settings.launch is not None:
            settings.launch.listen()
        self._show_status()
        self.first_run_changed()

    def _teardown(self) -> None:
        for source_id in (self._first_run_idle, self._check_grace):
            if source_id:
                GLib.source_remove(source_id)
        self._first_run_idle = self._check_grace = 0
        self._state.close()
        self.setup_card.stop()
        settings = self.pages.get("settings")
        if isinstance(settings, SettingsPage) and isinstance(settings.setup, SetupSection):
            settings.setup.stop()
        if isinstance(settings, SettingsPage) and settings.launch is not None:
            settings.launch.close()  # as classic Settings does
        self._prefs.close()
        provider, self._thumbnails = self._thumbnails, None
        if provider is not None:
            thumbs.uninstall(provider)
            provider.shutdown()

    @property
    def preferences(self) -> UiPrefsKeeper:
        """The window's ``ui.toml`` keeper."""
        return self._prefs

    # -- what the window shows, for tests and assistive descriptions ---------
    def _on_state_changed(self, _state: RealAppState, topic: str) -> None:
        if topic in ("now", "playback", "system", "displays"):
            self._show_status()

    def _show_status(self) -> None:
        self.playerbar.set_detail(status_line.describe(self._application.status_model.view))

    @property
    def status_text(self) -> str:
        """The live runtime status line (the player bar's detail), as shown."""
        return self.playerbar.detail

    @property
    def library_text(self) -> str:
        """How big the shown library is, e.g. ``2 wallpapers in the library``."""
        count = len(self._state.wallpapers)
        text = f"{count} wallpaper{'' if count == 1 else 's'} in the library"
        return text + " · scanning…" if self._state.library_scanning else text

    @property
    def note_text(self) -> str:
        """What a not-ported page says, while one is shown; otherwise empty."""
        page = self.current_page
        if isinstance(page, Placeholder):
            return page.status.get_description() or ""
        return ""

    # -- WindowServices: lifecycle ------------------------------------------------
    def report(self, message: str) -> None:
        """Show one transient message as a toast."""
        self.toasts.add_toast(plain_toast(message))

    def show_page(self, page: str) -> bool:
        """Show ``page``; False (with its placeholder shown) when it is not ported yet."""
        target = _PAGES.get(page)
        if target is None:
            return False
        self.navigate(target)
        return page in PORTED

    # -- WindowServices: settings and theme ---------------------------------------
    def apply_settings(self, settings: config.Settings) -> None:
        """Adopt a durable (or rolled-back) snapshot: every Settings row shows it."""
        self._settings = settings
        self._state.settings_changed()
        self.first_run_changed()  # a library folder chosen, or a choice rolled back

    @property
    def settings(self) -> config.Settings:
        return self._settings

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        """The application already applied the stylesheet; previews follow the mode."""
        self._state.emit_changed("theme")

    def open_tidy(self) -> None:
        """Settings, at Tidy up with the action the card offered open (its Review)."""
        self.navigate("settings:tidy")
        page = self.pages.get("settings")
        if isinstance(page, SettingsPage) and isinstance(page.tidy, TidySection):
            page.tidy.reveal()

    def open_setup(self) -> None:
        """Settings, at Setup (the setup card's Details)."""
        self.navigate("settings:setup")
        page = self.pages.get("settings")
        if isinstance(page, SettingsPage) and isinstance(page.setup, SetupSection):
            page.setup.reveal()

    def open_palette_browser(self) -> None:
        """Settings, with the palette manager open over it."""
        page = self.pages.get("settings")
        if isinstance(page, SettingsPage) and page.access is not None:
            self.navigate("settings:palettes")
        else:
            self.report(f"Palettes are not in the new interface yet. {CLASSIC_HINT}")

    # -- WindowServices: library and authoring -------------------------------------
    def show_library(self, session: Session) -> None:
        """Show the newly installed library, its favourites and playlists."""
        self._state.reload()
        # Whether it has videos or scenes decides whether no renderer at all
        # is worth the setup card.
        self.setup_card.library_changed()

    def show_library_scanning(self, scanning: bool) -> None:
        """Mark the Library while a scan runs; the shown cards stay."""
        self._state.set_scanning(scanning)

    def show_current(self, session: Session) -> None:
        """What plays is the runtime's answer; re-resolve it against the session."""
        self._state.refresh_current()

    def playlists_changed(self, session: Session) -> None:
        """A playlist store mutation: the sidebar and the on-screen marks."""
        self._state.reload_playlists()

    def pairing_health_changed(self, session: Session) -> None:
        """Wallpapers newly marked or cleared as unplayable."""
        self._state.reload()

    # -- WindowServices: runtime status, rendered from the model subscription ------
    def show_runtime_status(self, status: dict[str, object]) -> None:
        """Rendered from the status model; a new renderer or display error is also said.

        Once per error, by the classic window's rule: a renderer's
        ``last_error`` first, else ``output_discovery_error``; the same error
        again says nothing until it clears or the service goes away.
        """
        if not isinstance(status.get("playlist"), str) or not isinstance(status.get("source"), str):
            return
        self._protocol_error = ""
        last_error = status.get("last_error")
        output_error = status.get("output_discovery_error")
        if isinstance(last_error, str) and last_error:
            if last_error != self._runtime_error:
                self.report(last_error)
            self._runtime_error = last_error
        elif isinstance(output_error, str) and output_error:
            if output_error != self._runtime_error:
                self.report(f"Display discovery is degraded: {output_error}")
            self._runtime_error = output_error
        else:
            self._runtime_error = ""

    def show_runtime_unavailable(self) -> None:
        """Rendered from the status model; an error seen before may be said again later."""
        self._runtime_error = ""
        self._protocol_error = ""

    def show_runtime_delayed(self) -> None:
        """Rendered from the status model (the bar's "status delayed"); nothing more to do."""

    def show_runtime_protocol_error(self, message: str) -> None:
        """The bar marks the reply invalid; the reason is also said, once, as classic does."""
        if message != self._protocol_error:
            self.report(message)
        self._protocol_error = message

    def set_runtime_busy(self, busy: bool) -> None:
        """Rendered from the status model; nothing more to do."""

    # -- FirstRunHost: the welcome screen -------------------------------------------
    @property
    def welcome_shown(self) -> bool:
        """The welcome screen is up, in place of the sidebar, pages and player bar."""
        return self._root is not None and self._root.get_visible_child_name() == "welcome"

    def ask(self, question: Adw.AlertDialog) -> None:
        """Show the application's question on the welcome screen.

        The older-data check's progress at startup stays out of sight unless
        it runs long (`CHECK_GRACE_MS`); a question that wants an answer, or
        progress after one, is shown at once.
        """
        self._end_grace()
        self.welcome.show(question)
        if is_progress(question) and not self.welcome_shown:
            self._check_grace = GLib.timeout_add(CHECK_GRACE_MS, self._check_overdue, question)
        else:
            self._reveal_welcome()
            self.welcome.focus()
        self._refresh_window_banner()

    def publication_changed(self) -> None:
        """The last publication's problem, or what it left out, appeared or went: say so
        under the header."""
        self._refresh_window_banner()

    def first_run_changed(self) -> None:
        """Read the application again, once, after the callback that moved it.

        A question often closes and the next one opens in the same callback;
        settling afterwards keeps the welcome screen from flashing between.
        """
        if not self._first_run_idle:
            self._first_run_idle = GLib.idle_add(  # type: ignore[call-arg]
                self._settle_first_run, priority=GLib.PRIORITY_DEFAULT
            )

    def _settle_first_run(self) -> bool:
        self._first_run_idle = 0
        self._sync_first_run()
        return GLib.SOURCE_REMOVE

    def _sync_first_run(self) -> None:
        application = self._application
        question = application.first_run_question
        if question is not None:
            # A new question; or this one, out of sight with no grace period
            # running for it (the window went away meanwhile).
            if question is not self.welcome.question or not (
                self.welcome_shown or self._check_grace
            ):
                self.ask(question)
        elif self.welcome_shown and application.first_run_pending:
            self.welcome.show_waiting()
        else:
            self._end_grace()
            self._hide_welcome()
        self._refresh_window_banner()

    def _answer(self, question: Adw.AlertDialog, response: str) -> None:
        """A welcome button: answer the application's dialog, unless it moved on."""
        if self._application.first_run_question is not question:
            return
        question.emit("response", response)
        self._sync_first_run()

    def _check_overdue(self, question: Adw.AlertDialog) -> bool:
        self._check_grace = 0
        if self._application.first_run_question is question:
            self._reveal_welcome()
        return GLib.SOURCE_REMOVE

    def _end_grace(self) -> None:
        if self._check_grace:
            GLib.source_remove(self._check_grace)
            self._check_grace = 0

    def _reveal_welcome(self) -> None:
        """Cover the window with the welcome screen; its shortcuts wait, as under a dialog."""
        if self._root is not None and not self.welcome_shown:
            self._root.set_visible_child_name("welcome")
        self.block_actions(True)

    def _hide_welcome(self) -> None:
        if self._root is not None and self.welcome_shown:
            self._root.set_visible_child_name("main")
        self.block_actions(False)
        self.welcome.show_waiting()

    # -- the window's own notices ------------------------------------------------------
    def _window_banner(self) -> Banner | None:
        """What the window says under its header about first run and the gate."""
        application = self._application
        if application.first_run_question is not None:
            return None  # the welcome screen is asking
        gate = application.authoring_gate
        if gate == "decision":
            return Banner(OLDER_DATA_WAITING, "Review\u2026", OLDER_DATA)
        if gate == "probe-failed":
            return Banner(OLDER_DATA_UNCHECKED, "Try Again", OLDER_DATA)
        if gate == "repair":
            reason = application.authoring_gate_reason
            if reason:
                return Banner(f"{NOT_OPENED} {reason}", "Try Again", REPAIR)
            return Banner(REPAIR_NEEDED, "Try Again", REPAIR)
        if (
            gate == "open"
            and application.first_activation_done
            and not application.settings.roots
            and not application.requested_settings.roots  # one is being saved
        ):
            return Banner(NO_FOLDER, "Choose a Folder\u2026", LIBRARY_FOLDER)
        problem = application.publication_problem
        if problem:
            # Until a change gets through.
            return Banner(f"{PUBLICATION_FAILED} {problem}", "Playlists", PLAYLISTS)
        left_out = application.left_out_notice
        if left_out:
            # Published, without these: for as long as the playlist they name
            # doesn't exist or has nothing to play. Its page is where to fix it.
            return Banner(left_out, "Playlists", PLAYLISTS)
        return None

    def _refresh_window_banner(self) -> None:
        self._state.set_window_banner(self._window_banner(), self._on_window_banner)

    def _on_window_banner(self, action: str) -> None:
        application = self._application
        if action == OLDER_DATA:
            # Asked for: show the check as it runs, then the question.
            self.welcome.show_waiting()
            self._reveal_welcome()
            if not application.reconsider_older_data():
                self._sync_first_run()
        elif action == REPAIR:
            application.retry_authoring_repair()
            self._refresh_window_banner()
        elif action == LIBRARY_FOLDER and not application.ask_for_library_folder():
            self._sync_first_run()
        elif action == PLAYLISTS:
            self.navigate("playlist")
