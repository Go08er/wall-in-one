"""The window's setup notice and Settings → Setup, the same widgets in both interfaces.

What is missing, and every word said about it, comes from
`wall_in_one.setup_check` (docs/missing-components.md); this module only
shows it. The programs, the service's unit and the companion are looked for on
one ordered worker (`TidyLane`: the application's ``setup_lane``), whose
results reach GTK at ``GLib.PRIORITY_DEFAULT``. The service's state is the
status the application already polls (`RuntimeStatusModel`), so nothing is
said about the service before its first answer, and a service the companion
starts a moment later takes the finding away by itself.

**Check Again** asks the application to compile ``runtime.toml`` as a save
does and reload the service whatever changed (`Application.check_setup_again`),
then looks again. **Not Now** hides the findings the card showed for the rest
of the session; the dismissal is kept on the application (``setup_dismissed``)
and nothing is written. A finding that changes shows again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk

from wall_in_one import install_kind, setup_check
from wall_in_one.setup_check import Finding, Local, Service
from wall_in_one.ui.plain_text import plain
from wall_in_one.ui.status_model import RuntimeStatusView, StatusChange
from wall_in_one.ui.tidy_section import TidyLane

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

#: What the companion's link says.
COMPANION_LINK_LABEL = "Get the Companion Plugin"

#: Called with the findings (None until the first look landed, or after a
#: look that failed) and why a look failed.
Shown = Callable[[tuple[Finding, ...] | None, str], None]


def service_from(view: RuntimeStatusView | None) -> Service:
    """The service as the application's polled status has it.

    ``checking`` is not an answer yet, so nothing is said about it.
    """
    if view is None or view.service == "checking":
        return Service("unknown")
    if view.service == "unavailable":
        return Service("not-running")
    return setup_check.service_from_status(view.status)


def _dismissed(application: Application) -> set[str] | None:
    kept = getattr(application, "setup_dismissed", None)
    return kept if isinstance(kept, set) else None


class SetupWatch:
    """Keeps one widget's findings current: local looks on a worker, the service from status."""

    def __init__(self, application: Application, shown: Shown) -> None:
        self._app = application
        self._shown = shown
        self._lane: TidyLane | None = None
        self._local: Local | None = None
        self._service = Service("unknown")
        self._unsubscribe: Callable[[], None] | None = None
        self._looking = False
        self._again = False
        #: After `stop`: a look still on the lane lands nowhere.
        self._stopped = False
        self.findings: tuple[Finding, ...] | None = None
        self.error = ""

    @property
    def looking(self) -> bool:
        return self._looking

    def start(self) -> None:
        """Follow the service's status, and look at this machine."""
        self._stopped = False
        if self._unsubscribe is None:
            model = getattr(self._app, "status_model", None)
            if model is not None:
                self._service = service_from(model.view)
                self._unsubscribe = model.subscribe(self._on_status)
        self.look()

    def look(self) -> None:
        """Look again in the background; asked while looking, once more after."""
        if self._looking:
            self._again = True
            return
        lane = self._lane_now()
        self._looking = True
        if not lane.submit(setup_check.local, self._landed):
            self._looking = False

    def stop(self) -> None:
        self._stopped = True
        self._again = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._lane is not None and self._lane is not getattr(self._app, "setup_lane", None):
            self._lane.shutdown()
        self._lane = None
        self._looking = False

    def _lane_now(self) -> TidyLane:
        if self._lane is None:
            lane = getattr(self._app, "setup_lane", None)
            self._lane = lane if isinstance(lane, TidyLane) else TidyLane(stopped="Couldn't check")
        return self._lane

    def _landed(self, local: Local | None, error: str) -> None:
        self._looking = False
        if self._stopped:
            return
        # A look that failed says so, rather than what an older look found.
        self._local = local
        self.error = "" if local is not None else error or "Couldn't check"
        self._emit()
        if self._again:
            self._again = False
            self.look()

    def _on_status(self, _change: StatusChange, view: RuntimeStatusView) -> None:
        service = service_from(view)
        if service != self._service:
            self._service = service
            if self._local is not None:
                self._emit()

    def _emit(self) -> None:
        if self._local is None:
            self.findings = None
        else:
            self.findings = setup_check.findings(
                install_kind.INSTALL_KIND, self._local, self._service
            )
        self._shown(self.findings, self.error)


def _label(text: str, *css: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=True)
    for name in css:
        label.add_css_class(name)
    return label


def _link() -> Gtk.LinkButton:
    link = Gtk.LinkButton(uri=setup_check.COMPANION_URL, label=COMPANION_LINK_LABEL)
    link.set_tooltip_text(setup_check.COMPANION_URL)
    link.set_valign(Gtk.Align.CENTER)
    return link


class SetupCard(Gtk.Revealer):
    """Above every page while something required is missing, in both interfaces."""

    def __init__(self, application: Application, details: Callable[[], None]) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._app = application
        self._details = details
        self._own_dismissed: set[str] = set()
        self._watch = SetupWatch(application, self._show)
        #: The findings the card shows now.
        self.shown: tuple[Finding, ...] = ()
        box = Gtk.Box(spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        box.add_css_class("card")
        inner = Gtk.Box(spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{side}")(12)
        icon = Gtk.Image(icon_name="dialog-warning-symbolic", pixel_size=32)
        icon.set_valign(Gtk.Align.START)
        inner.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.title = _label(setup_check.NOTICE_TITLE, "heading")
        text.append(self.title)
        self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        text.append(self.items)
        self.link = _link()
        self.link.set_halign(Gtk.Align.START)
        self.link.set_visible(False)
        text.append(self.link)
        inner.append(text)
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.not_now = Gtk.Button(label="Not Now")
        self.not_now.connect("clicked", lambda _button: self.dismiss())
        self.check_again = Gtk.Button(label="Check Again")
        self.check_again.connect("clicked", lambda _button: self.check())
        self.details = Gtk.Button(label="Details")
        self.details.add_css_class("suggested-action")
        self.details.connect("clicked", lambda _button: self._details())
        for button in (self.not_now, self.check_again, self.details):
            buttons.append(button)
        inner.append(buttons)
        box.append(inner)
        self.set_child(box)
        self.set_reveal_child(False)
        self.connect("unrealize", lambda _widget: self._watch.stop())

    @property
    def watch(self) -> SetupWatch:
        return self._watch

    def start(self) -> None:
        """When the window first appears: follow the service and look once."""
        self._watch.start()

    def stop(self) -> None:
        """With its window's teardown: stop following the service.

        The window calls it from its own ``unrealize`` teardown, because the
        card's own ``unrealize`` does not come for a card never shown.
        """
        self._watch.stop()

    def check(self) -> None:
        """Check Again: compile and reload through the application, then look again."""
        self.check_again.set_sensitive(False)
        again = getattr(self._app, "check_setup_again", None)
        if callable(again):
            again()
        self._watch.look()

    def dismiss(self) -> None:
        """Not Now: these findings, said this way, stay hidden for the session."""
        dismissed = _dismissed(self._app)
        (self._own_dismissed if dismissed is None else dismissed).update(
            finding.fingerprint for finding in self.shown
        )
        self.shown = ()
        self.set_reveal_child(False)

    def _show(self, findings: tuple[Finding, ...] | None, _error: str) -> None:
        self.check_again.set_sensitive(not self._watch.looking)
        if findings is None:
            # A look that failed says so in Settings; the card says nothing.
            return
        kept = _dismissed(self._app)
        dismissed = self._own_dismissed if kept is None else kept
        shown = tuple(
            finding
            for finding in setup_check.required(findings)
            if finding.fingerprint not in dismissed
        )
        self.shown = shown
        child = self.items.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.items.remove(child)
            child = following
        for finding in shown:
            self.items.append(_label(f"{finding.title}. {finding.fix}"))
        self.link.set_visible(any(finding.link for finding in shown))
        self.set_reveal_child(bool(shown))


class SetupSection(Adw.PreferencesGroup):
    """Settings → Setup: every finding, required and optional, with Check Again."""

    def __init__(self, application: Application) -> None:
        super().__init__(
            title=setup_check.SECTION_TITLE,
            description=setup_check.SECTION_DESCRIPTION,
        )
        self._app = application
        self._watch = SetupWatch(application, self._show)
        self._rows: list[Gtk.Widget] = []
        self.check_again = Gtk.Button(label="Check Again", valign=Gtk.Align.CENTER)
        self.check_again.add_css_class("flat")
        self.check_again.connect("clicked", lambda _button: self.check())
        self.set_header_suffix(self.check_again)
        self._replace([self._row("Checking…")])
        # Each visit shows what is there now; looking only reads.
        self.connect("map", lambda _widget: self._watch.start())
        self.connect("unrealize", lambda _widget: self._watch.stop())

    @property
    def watch(self) -> SetupWatch:
        return self._watch

    @property
    def rows(self) -> tuple[Gtk.Widget, ...]:
        return tuple(self._rows)

    def stop(self) -> None:
        """With its window's teardown, as the card's."""
        self._watch.stop()

    def check(self) -> None:
        """Check Again, as the card's."""
        self.check_again.set_sensitive(False)
        again = getattr(self._app, "check_setup_again", None)
        if callable(again):
            again()
        self._watch.start()

    def reveal(self) -> None:
        """Bring the section into view (the card's Details)."""
        self.check_again.grab_focus()

    @staticmethod
    def _row(title: str, subtitle: str = "", icon: str = "") -> Adw.ActionRow:
        row: Adw.ActionRow = plain(Adw.ActionRow, title=title, subtitle=subtitle)
        row.set_title_lines(0)
        row.set_subtitle_lines(0)
        row.set_activatable(False)
        if icon:
            row.add_prefix(Gtk.Image(icon_name=icon))
        return row

    def _replace(self, rows: list[Gtk.Widget]) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows = rows
        for row in rows:
            self.add(row)

    def _show(self, findings: tuple[Finding, ...] | None, error: str) -> None:
        self.check_again.set_sensitive(not self._watch.looking)
        if findings is None:
            self._replace([self._row(error or "Couldn't check", icon="dialog-warning-symbolic")])
            return
        if not findings:
            self._replace([self._row(setup_check.ALL_PRESENT, icon="emblem-ok-symbolic")])
            return
        rows: list[Gtk.Widget] = []
        for finding in findings:
            row = self._row(
                finding.title,
                f"{finding.detail} {finding.fix}",
                "dialog-warning-symbolic" if finding.required else "dialog-information-symbolic",
            )
            if finding.link:
                row.add_suffix(_link())
            rows.append(row)
        self._replace(rows)
