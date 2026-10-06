"""Applications started as GTK requires before a window is added, and no further.

GTK only adds a window to an application that has emitted ``startup``
("New application windows must be added after the GApplication::startup
signal has been emitted", a Gtk-CRITICAL, and the window isn't added). A
test that builds a window without running the application used to do it
over an unstarted one. These start it as a private, non-unique instance:
GTK's startup runs, nothing is registered on a bus, and nothing is left
running.
"""

from __future__ import annotations

from gi.repository import Adw, Gio

from wall_in_one.ui.app import Application
from wall_in_one.ui.window_services import UiKind


class StartedApplication(Application):
    """The real `Application`, started for GTK, without the process's own startup.

    `Application.do_startup` also binds the control socket, watches the
    palette, installs accelerators, installs the durability sink and queues
    the older-data check: a running process's work, which nothing would shut
    down after a test that only builds a window.
    """

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)


def started[Started: Gio.Application](application: Started) -> Started:
    """Start ``application`` as a private, non-unique instance, without running it."""
    application.set_flags(application.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    if not application.register(None):  # pragma: no cover - a broken GLib
        raise AssertionError("the application could not be started")
    assert application.get_is_registered()
    return application


def started_application(ui: UiKind, *, first_activation_done: bool = False) -> Application:
    """A real `Application` a test can build its window over (`StartedApplication`).

    A started application's first window on a fresh profile opens on the
    welcome screen, its actions waiting for the first question
    (`NextWindow`), as the running app's does. ``first_activation_done``
    is a later window instead, the process past its first activation.
    """
    application = started(StartedApplication(ui=ui))
    application._first_activation_done = first_activation_done
    return application
