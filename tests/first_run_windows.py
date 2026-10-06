"""The first-run questions in either window, for tests that run against both.

The application asks the same questions -- the older-data check and the first
library folder -- as one ``Adw.AlertDialog`` each. The classic window presents
the dialog; the new one shows it on its welcome screen and answers it with the
welcome screen's buttons. These helpers let one test ask and answer through
whichever window it was given, and always name the interface explicitly
(``Application(ui=...)``) rather than rely on the default.
"""

from __future__ import annotations

from typing import Literal, cast

import pytest
from gi.repository import Adw, Gtk

from tests.gtk_helpers import spin_until
from tests.started import started_application
from wall_in_one.ui.app import Application
from wall_in_one.ui.next.window import NextWindow
from wall_in_one.ui.window import MainWindow

Ui = Literal["classic", "next"]
UIS: tuple[Ui, ...] = ("classic", "next")


def application_with_window(ui: Ui) -> tuple[Application, Gtk.Window]:
    """An application and the window its questions go to.

    Classic keeps these tests' long-standing stand-in, a plain window to
    present dialogs over. The new interface gets its real window, shown, since
    the welcome screen is part of it.
    """
    application = started_application(ui)
    window: Gtk.Window
    if ui == "next":
        window = NextWindow(application, application.settings)
        application._window = window
        window.present()
    else:
        window = Gtk.Window()
        application._window = cast(MainWindow, window)
    return application, window


def capture_questions(monkeypatch: pytest.MonkeyPatch) -> list[Adw.AlertDialog]:
    """Every first-run question asked, presented as a dialog or on the welcome screen."""
    asked: list[Adw.AlertDialog] = []
    monkeypatch.setattr(
        Adw.AlertDialog,
        "present",
        lambda dialog, _parent: asked.append(dialog),
    )
    real_ask = NextWindow.ask

    def ask(window: NextWindow, question: Adw.AlertDialog) -> None:
        asked.append(question)
        real_ask(window, question)

    monkeypatch.setattr(NextWindow, "ask", ask)
    return asked


def respond(window: Gtk.Window, question: Adw.AlertDialog, response: str) -> None:
    """Answer ``question`` the way the user would in ``window``.

    On the welcome screen that is its button, which must be showing, with the
    dialog's own label; elsewhere it is the dialog's response.
    """
    if isinstance(window, NextWindow):
        spin_until(
            lambda: window.welcome_shown and window.welcome.question is question,
            what=f"the welcome screen asking {question.get_heading()!r}",
        )
        button = window.welcome.buttons[response]
        assert button.get_label() == question.get_response_label(response)
        button.emit("clicked")
    else:
        question.emit("response", response)


def close_window(window: Gtk.Window) -> None:
    window.destroy()
