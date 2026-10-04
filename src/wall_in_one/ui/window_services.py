"""What `Application` needs from its window, whichever window that is.

`Application` used to drive `MainWindow` by duck typing: about sixteen
methods, called from the status poll, the authoring lanes, the library scan,
the palette worker and the control socket. A second window that lacked any of
them would raise `AttributeError` on the first two-second status poll. Typing
the application's window reference as this Protocol makes mypy prove that the
application calls nothing else, and that every window implements all of it.

Three things deliberately stay out:

* ``connect("close-request", …)`` is called on the concrete window class
  before it is stored, so it needs no Protocol member.
* Dialogs and file choosers want a real `Gtk.Window` parent. The application
  narrows with ``isinstance`` instead, which keeps the lightweight test
  doubles (plain `Gtk.Window` objects, or no GTK object at all) working.
* The pages' own collaborators (`AppServices` in the R9 notes) are a separate
  seam: pages talk to the application, never to this Protocol.

One capability is optional, so it is its own Protocol: `FirstRunHost`, a window
that asks the first-run questions on its own welcome screen. The application
checks for it with ``isinstance`` and presents its dialogs over any other
window, so the classic window, and the plain test windows, are unchanged.

Which window gets built is the process's ``--ui`` choice (`UiKind`), else the
interface chosen in Settings, else `DEFAULT_UI`: since 0.3.0 the new
interface (``next``, `NextWindow`). ``classic`` builds `MainWindow`.

GTK-free on purpose: annotations only, so the command line can import the
choices without loading GTK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from gi.repository import Adw

    from wall_in_one import config
    from wall_in_one.session import Session
    from wall_in_one.theme import source

#: The window a process builds: the classic interface, or the new one.
UiKind = Literal["classic", "next"]
UI_KINDS: Final[tuple[UiKind, ...]] = ("classic", "next")
#: Built when neither ``--ui`` nor Settings chose: the ui_prefs module's ``DEFAULT_INTERFACE``.
DEFAULT_UI: Final[UiKind] = "next"


class WindowServices(Protocol):
    """Every member `Application` and the control layer use on their window."""

    # -- lifecycle --------------------------------------------------------

    def present(self) -> None:
        """Raise the window to the user; GTK's own ``Gtk.Window.present``."""

    def report(self, message: str) -> None:
        """Show one short, transient message (a toast); never blocks or asks."""

    def show_page(self, page: str) -> bool:
        """Navigate to a validated page name; False if this window has no such page yet."""

    # -- settings and theme ----------------------------------------------

    def apply_settings(self, settings: config.Settings) -> None:
        """Adopt a durable (or rolled-back) settings snapshot into every control."""

    def show_palette(self, resolved: source.ResolvedPalette) -> None:
        """React to a newly installed app palette; the stylesheet is already applied."""

    def open_palette_browser(self) -> None:
        """Open the palette chooser, from a menu, a shortcut or another page."""

    # -- library and authoring -------------------------------------------

    def show_library(self, session: Session) -> None:
        """Rebuild library views from a newly installed `Session.library`."""

    def show_library_scanning(self, scanning: bool) -> None:
        """Show or clear the scan-in-progress state; keep existing views mounted."""

    def show_current(self, session: Session) -> None:
        """Refresh what is playing and highlighted, without rebuilding the library."""

    def playlists_changed(self, session: Session) -> None:
        """Refresh playlist views after a playlist-store mutation."""

    def pairing_health_changed(self, session: Session) -> None:
        """Reflect wallpapers newly marked or cleared as unplayable."""

    # -- runtime status -------------------------------------------------

    def show_runtime_status(self, status: dict[str, object]) -> None:
        """Render one valid atomic runtime snapshot; clears delayed/invalid state."""

    def show_runtime_unavailable(self) -> None:
        """Say the wallpaper runtime is not running; authoring still works."""

    def show_runtime_delayed(self) -> None:
        """Keep the last snapshot, marked stale, after one missed status deadline."""

    def show_runtime_protocol_error(self, message: str) -> None:
        """Keep the last snapshot, marked invalid, after a malformed status reply."""

    def set_runtime_busy(self, busy: bool) -> None:
        """Show whether the single in-flight playback command is still running."""


@runtime_checkable
class FirstRunHost(Protocol):
    """A window that asks the first-run questions itself, in place of dialogs.

    Optional, unlike `WindowServices`: the application checks for it with
    ``isinstance`` and presents its dialogs over any other window, as it always
    has. The questions are the older-data check (its progress, its choice and
    its failure) and the library folder. Each is the application's own
    ``Adw.AlertDialog``, with the same heading, body and responses; the window
    shows it inside itself and answers by emitting that dialog's ``response``
    signal, so the handlers, guards and workers that act on the answer are the
    ones every window uses.
    """

    def ask(self, question: Adw.AlertDialog) -> None:
        """Show ``question`` now, in place of whatever question was showing."""

    def first_run_changed(self) -> None:
        """A question closed, or the authoring gate moved: read the application again."""
