"""The new interface's window: the redesign's shell over the application's real data.

`NextWindow` is the prototype's `ShellWindow` -- sidebar, header, banners,
player bar, frosted backdrop -- over a `RealAppState`, with the real Library
page and inspector, the real Playlists page, and the real Settings page
(`settings.SettingsPage`, with the palette manager), which also offers the
interface to start, so the new one can be left from inside. Store, Schedule
and Displays are honest "not in the new interface yet" pages until they are
ported.

It implements every `WindowServices` member. Runtime status reaches it
through the adapter's own `RuntimeStatusModel` subscription, so the forwarded
``show_runtime_*`` calls are no-ops; library, playlist and health changes
arrive as the application hands over its `Session`. Pictures come from the
app's thumbnail pipeline (`LibraryThumbnails`). The window style and its
dials come from ``ui.toml`` and are rendered by the application into its own
stylesheet (`Application.set_window_glass`).

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

from gi.repository import Adw

from wall_in_one import __version__, config
from wall_in_one.session import Session
from wall_in_one.theme import source
from wall_in_one.ui.launch_rows import LaunchRows
from wall_in_one.ui.next import library, playlists, status_line, store, thumbs
from wall_in_one.ui.next.catalog import CLASSIC_HINT
from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
from wall_in_one.ui.next.page import Placeholder, not_ported
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_state import RealAppState
from wall_in_one.ui.next.settings import SettingsPage
from wall_in_one.ui.next.shell import ShellWindow
from wall_in_one.ui.next.state import AppState
from wall_in_one.ui.tidy_section import TidySection

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: `show_page` names (as ``ctl open`` and ``--open-page`` give them) -> shell pages.
_PAGES: Final = {
    "media": "library",
    "browse": "store",
    "playlists": "playlist",
    "schedules": "schedule",
    "settings": "settings",
}
#: The pages this interface has. Every other one is a placeholder for now.
PORTED: Final = frozenset({"media", "browse", "playlists", "settings"})
#: Kept for callers of the placeholder era.
NOT_PORTED_HINT: Final = CLASSIC_HINT


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
        self._thumbnails: LibraryThumbnails | None = LibraryThumbnails()
        thumbs.install(self._thumbnails)
        self._prefs = UiPrefsKeeper(application.window_report)
        self._state = RealAppState(application, self._prefs)
        application.set_window_glass(self._state.glass())
        super().__init__(
            application,
            self._state,
            {
                "library": library.create,
                "store": store.create,
                "playlist": playlists.create,
                "schedule": not_ported("schedule", "Schedule", "x-office-calendar-symbolic"),
                "displays": not_ported("displays", "Displays", "video-display-symbolic"),
                "settings": lambda state: settings_page(state, self._prefs, application),
            },
            version=__version__,
        )
        self._state.connect("changed", self._on_state_changed)
        self._show_status()
        self.connect("realize", lambda _window: self._resume())
        self.connect("unrealize", lambda _window: self._teardown())

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

    def _teardown(self) -> None:
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
