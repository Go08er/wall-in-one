"""The new interface's welcome screen: the first-run questions, asked in the window.

The questions are the application's own (``Application._ask``):

* the check for settings and playlists from the older Noctalia plugin -- its
  progress, its choice ("Import safely", "Start fresh" or "Keep current",
  "Resume import", "Not now") and its failure ("Try again", "Not now");
* the first library folder: "Use default" for the exact folder found on this
  computer, "Choose folder manually", or "Not now".

Each arrives as the application's ``Adw.AlertDialog`` and is shown here with
its own heading, body and response labels, so both interfaces ask the same
question in the same words. A button emits that dialog's ``response``; the
application's handler does the rest, exactly as for the classic window's
dialogs. Nothing here decides or writes anything.

A folder found on this computer is a suggestion, not consent: the body names
it, and only "Use default" saves it. "Not now" saves nothing, and the question
comes back on the next start, or from the window's notice before then.

The look is the design prototype's welcome (``prototypes/ui-redesign``): a
status page with pill buttons under a flat header bar, one solid background in
every window style.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gtk, Pango

#: The application's response ids, in the order the buttons are offered after
#: the suggested one. A dialog has a subset of them.
RESPONSES: Final = ("import", "retry", "default", "manual", "fresh", "later")
#: The progress dialogs' one, always disabled, response.
WORKING: Final = "working"
APP_ICON: Final = "dev.goober.WallInOne"
WELCOME: Final = "Welcome to Wall-in-One"
GETTING_READY: Final = "Getting ready…"

#: Called with the question and the chosen response id.
Answer = Callable[[Adw.AlertDialog, str], None]


def is_progress(question: Adw.AlertDialog) -> bool:
    """A dialog that only shows work under way: it can't be closed or answered."""
    return not question.get_can_close()


class Welcome:
    """One question at a time, or "getting ready" between them."""

    def __init__(self, answer: Answer) -> None:
        self._answer = answer
        #: The question shown, or None while getting ready.
        self.question: Adw.AlertDialog | None = None
        #: The response buttons shown now, by response id.
        self.buttons: dict[str, Gtk.Button] = {}

        self.status = Adw.StatusPage(icon_name=APP_ICON, title=WELCOME)
        # The body is plain text with paths in it: never markup.
        self.body = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.CENTER,
            selectable=True,
            max_width_chars=64,
            use_markup=False,
        )
        self.body.add_css_class("body")
        self.spinner = Adw.Spinner(halign=Gtk.Align.CENTER, visible=False)
        self.spinner.set_size_request(32, 32)
        self._buttons = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.append(self.body)
        box.append(self.spinner)
        box.append(self._buttons)
        # A readable measure for the body; a long path still wraps.
        self.status.set_child(Adw.Clamp(maximum_size=640, child=box))

        # A flat header bar: window controls, and something to drag the window by.
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Adw.WindowTitle(title=WELCOME))
        view = Adw.ToolbarView(content=self.status)
        view.add_top_bar(header)
        view.add_css_class("background")  # one solid layer in every window style
        self.widget: Gtk.Widget = view
        self.show_waiting()

    # -- what is shown ---------------------------------------------------------------
    @property
    def title_text(self) -> str:
        return self.status.get_title() or ""

    @property
    def body_text(self) -> str:
        return self.body.get_label() or ""

    @property
    def waiting(self) -> bool:
        """The spinner is shown: work is under way and nothing can be answered."""
        return self.spinner.get_visible()

    def show_waiting(self) -> None:
        """Between questions: the application is still checking or repairing."""
        self.question = None
        self._fill(WELCOME, GETTING_READY, waiting=True)

    def show(self, question: Adw.AlertDialog) -> None:
        """Show ``question``: its heading, body and responses, as the application wrote them."""
        if question is self.question:
            return
        self.question = question
        heading = question.get_heading() or WELCOME
        if is_progress(question):
            self._fill(heading, question.get_body(), waiting=True)
            return
        self._fill(heading, question.get_body(), waiting=False)
        offered = [
            response
            for response in RESPONSES
            if question.has_response(response) and question.get_response_enabled(response)
        ]
        default = question.get_default_response()
        if default in offered:
            offered.remove(default)
            offered.insert(0, default)
        for response in offered:
            button = Gtk.Button(label=question.get_response_label(response))
            button.add_css_class("pill")
            appearance = question.get_response_appearance(response)
            if response == default or appearance == Adw.ResponseAppearance.SUGGESTED:
                button.add_css_class("suggested-action")
            elif appearance == Adw.ResponseAppearance.DESTRUCTIVE:
                button.add_css_class("destructive-action")
            elif response == question.get_close_response():
                button.add_css_class("flat")
            button.connect(
                "clicked", lambda _button, chosen=response: self._answer(question, chosen)
            )
            self._buttons.append(button)
            self.buttons[response] = button

    def focus(self) -> None:
        """Put the keyboard on the first answer, the one offered first."""
        first = next(iter(self.buttons.values()), None)
        if first is not None:
            first.grab_focus()

    def _fill(self, title: str, body: str, *, waiting: bool) -> None:
        self.status.set_title(title)
        self.body.set_label(body)
        self.spinner.set_visible(waiting)
        child = self._buttons.get_first_child()
        while child is not None:
            self._buttons.remove(child)
            child = self._buttons.get_first_child()
        self.buttons = {}
