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
``show_runtime_*`` calls are no-ops; library, playlist and health changes
arrive as the application hands over its `Session`. Pictures come from the
app's thumbnail pipeline (`LibraryThumbnails`). The window style and its
dials come from ``ui.toml`` and are rendered by the application into its own
stylesheet (`Application.set_window_glass`).

**First run.** The window asks the application's first-run questions on its
own welcome screen (`welcome.Welcome`) instead of in dialogs: it is a
`FirstRunHost`. The questions, their words and what each answer does are the
application's, shared with the classic window. A fresh profile opens on the
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

from typing import TYPE_CHECKING, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from wall_in_one import __version__, config
from wall_in_one.session import Session
from wall_in_one.theme import source
from wall_in_one.ui.launch_rows import LaunchRows
from wall_in_one.ui.next import displays, library, playlists, schedule, status_line, store, thumbs
from wall_in_one.ui.next.catalog import CLASSIC_HINT
from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
from wall_in_one.ui.next.page import Placeholder
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_state import RealAppState
from wall_in_one.ui.next.settings import SettingsPage
from wall_in_one.ui.next.shell import ShellWindow
from wall_in_one.ui.next.state import AppState, Banner
from wall_in_one.ui.next.welcome import Welcome, is_progress
from wall_in_one.ui.tidy_section import TidySection

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
OLDER_DATA_WAITING: Final = (
    "Settings and playlists from the older Noctalia plugin are waiting for your choice. "
    "Until then, library, playlist and settings changes can\u2019t be saved."
)
OLDER_DATA_UNCHECKED: Final = (
    "Wall-in-One couldn\u2019t check for settings from the older Noctalia plugin. "
    "Until it can, library, playlist and settings changes can\u2019t be saved."
)
REPAIR_NEEDED: Final = (
    "Wall-in-One couldn\u2019t repair references to playlists that are gone, so "
    "library, playlist and settings changes can\u2019t be saved."
)
NO_FOLDER: Final = (
    "There\u2019s no wallpaper folder yet, so the library is empty and nothing is downloaded."
)
OLDER_DATA: Final = "older-data"
REPAIR: Final = "repair"
LIBRARY_FOLDER: Final = "library-folder"


def settings_page(state: AppState, keeper: UiPrefsKeeper, application: Application) -> SettingsPage:
    """Settings over the application: its rows, the next start's interface, Tidy up.

    The interface and GPU rows (`LaunchRows`) write ui.toml through the
    window's keeper, like every window preference here, show what was saved,
    and are off while ui.toml cannot be written. Tidy up is the classic
    section itself, on the application's own lane.
    """
    return SettingsPage(
        state,
        launch=LaunchRows(keeper, application.window_report),
        tidy=TidySection(application),
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
        self._state.connect("changed", self._on_state_changed)
        self._show_status()
        self.connect("realize", lambda _window: self._resume())
        self.connect("unrealize", lambda _window: self._teardown())
        if not application.first_activation_done and not settings.roots:
            # A fresh profile: its first question follows at once, so open on
            # the welcome screen rather than flash the empty window first.
            self._reveal_welcome()
        # After the activation that builds this window has asked what it asks.
        self.first_run_changed()

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
        settings = self.pages.get("settings")
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
        self.toasts.add_toast(Adw.Toast.new(message))

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
        """Rendered from the status model; nothing more to do."""

    def show_runtime_unavailable(self) -> None:
        """Rendered from the status model; nothing more to do."""

    def show_runtime_delayed(self) -> None:
        """Rendered from the status model; nothing more to do."""

    def show_runtime_protocol_error(self, message: str) -> None:
        """Rendered from the status model; the classic window toasts, this one marks the bar."""

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
        if self._root is not None and not self.welcome_shown:
            self._root.set_visible_child_name("welcome")

    def _hide_welcome(self) -> None:
        if self._root is not None and self.welcome_shown:
            self._root.set_visible_child_name("main")
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
            return Banner(REPAIR_NEEDED, "Try Again", REPAIR)
        if (
            gate == "open"
            and application.first_activation_done
            and not application.settings.roots
            and not application.requested_settings.roots  # one is being saved
        ):
            return Banner(NO_FOLDER, "Choose a Folder\u2026", LIBRARY_FOLDER)
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
