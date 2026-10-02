"""Settings → Tidy up, and the one-time card that offers it after an update.

The work is :mod:`wall_in_one.tidy`. This module only shows its preview and
starts an action when the user asks for it, on one ordered worker lane whose
results reach GTK at ``GLib.PRIORITY_DEFAULT``: a plain idle would wait below
redraw while the row's spinner animates, so the spinner would never stop.

The card reads ``ui.toml`` and plans in the background when the window first
appears, and writes nothing. Only the user dismissing it (Review or Not now)
writes ``ui.toml``, once.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from wall_in_one import paths, tidy, ui_prefs

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application

_Result = TypeVar("_Result")

#: The button that starts each action. The cache's says what it does, since
#: it is the one action without an archive or an undo.
_APPLY_LABELS: dict[str, str] = {tidy.THUMBNAIL_CACHE: "Clear Cache"}
#: Grouped preview rows per action before the rest are summarized.
_MAX_GROUPS = 12


class TidyLane:
    """One ordered worker for Tidy up; each result is delivered on GTK."""

    def __init__(self) -> None:
        self._jobs: ThreadPoolExecutor | None = None
        self._closed = False
        self._pending = 0

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def submit(
        self,
        work: Callable[[], _Result],
        done: Callable[[_Result | None, str], None],
    ) -> bool:
        """Run ``work`` off the main loop; ``done(result, error)`` runs on it."""
        if self._closed:
            return False
        if self._jobs is None:
            self._jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tidy-up")
        try:
            future = self._jobs.submit(work)
        except RuntimeError:
            return False
        self._pending += 1
        future.add_done_callback(
            lambda finished: GLib.idle_add(  # type: ignore[call-arg]
                self._deliver, finished, done, priority=GLib.PRIORITY_DEFAULT
            )
        )
        return True

    def _deliver(
        self, future: Future[_Result], done: Callable[[_Result | None, str], None]
    ) -> bool:
        self._pending -= 1
        if self._closed or future.cancelled():
            return GLib.SOURCE_REMOVE
        try:
            result = future.result()
        except tidy.TidyError as error:
            done(None, str(error))
        except Exception as error:  # A worker failure must never escape the main loop.
            done(None, f"Tidy up stopped: {error}")
        else:
            done(result, "")
        return GLib.SOURCE_REMOVE

    def shutdown(self) -> None:
        """Drop queued work and wait for the running step, which is short.

        An apply in flight finishes rather than being abandoned under the
        transaction lock; nothing delivers to GTK afterwards.
        """
        self._closed = True
        if self._jobs is not None:
            self._jobs.shutdown(wait=True, cancel_futures=True)
            self._jobs = None


def _lane_of(application: Application) -> TidyLane | None:
    lane = getattr(application, "tidy_lane", None)
    return lane if isinstance(lane, TidyLane) else None


def _roots(application: Application) -> tuple[Path, ...]:
    settings = getattr(application, "settings", None)
    roots = getattr(settings, "roots", ())
    return tuple(roots)


def _home(path: Path) -> str:
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home) :] if home != "/" and text.startswith(home + "/") else text


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """A plain-text row: paths and Noctalia values are never markup."""
    row = Adw.ActionRow(use_markup=False, title=title, subtitle=subtitle)
    row.set_title_lines(0)
    row.set_subtitle_lines(0)
    row.set_activatable(False)
    return row


def _grouped(
    entries: Sequence[tuple[Path, str]],
) -> list[tuple[str, str, int]]:
    """(text, place, count) per distinct text and place, in first-seen order.

    The place is the folder an item is in, or the file itself when the
    change is an edit inside Noctalia's settings.
    """
    counts: dict[tuple[str, str], int] = {}
    edited = paths.noctalia_settings_path()
    for path, text in entries:
        key = (text, _home(path if path == edited else path.parent))
        counts[key] = counts.get(key, 0) + 1
    return [(text, folder, count) for (text, folder), count in counts.items()]


class _ActionRow:
    """One action's expander: summary, preview, kept items, Apply, Undo and Retry."""

    def __init__(self, section: TidySection, plan: tidy.ActionPlan) -> None:
        self.section = section
        self.plan = plan
        self.expander = Adw.ExpanderRow(use_markup=False, title=plan.title)
        self.expander.set_subtitle_lines(0)
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.apply = Gtk.Button(
            label=_APPLY_LABELS.get(plan.action, "Apply"), valign=Gtk.Align.CENTER
        )
        self.apply.add_css_class("suggested-action")
        self.apply.connect("clicked", lambda _button: section.run(self, undo=False))
        self.undo = Gtk.Button(label="Undo", valign=Gtk.Align.CENTER)
        self.undo.connect("clicked", lambda _button: section.run(self, undo=True))
        # A step after a change that hasn't finished yet, such as Noctalia's reload.
        self.retry = Gtk.Button(label="Retry", valign=Gtk.Align.CENTER)
        self.retry.connect("clicked", lambda _button: section.run(self, retry=True))
        # Offered after an Undo that couldn't put everything back: stop retrying.
        self.keep = Gtk.Button(label="Keep Archived", valign=Gtk.Align.CENTER)
        self.keep.connect("clicked", lambda _button: section.run(self, keep=True))
        for widget in (self.spinner, self.retry, self.keep, self.undo, self.apply):
            self.expander.add_suffix(widget)
        self.children: list[Gtk.Widget] = []
        self.show(plan)

    def show(self, plan: tidy.ActionPlan) -> None:
        self.plan = plan
        for child in self.children:
            self.expander.remove(child)
        self.children = []
        self.expander.set_subtitle(plan.summary)
        if plan.blocked:
            self._add(_row("Not now", plan.blocked))
        changes = [(change.path, change.detail) for change in plan.changes]
        groups = _grouped(changes)
        for text, folder, count in groups[:_MAX_GROUPS]:
            title = f"{count} items: {text}" if count > 1 else text[:1].upper() + text[1:]
            self._add(_row(title, f"In {folder}"))
        if len(groups) > _MAX_GROUPS or any(count > 1 for _text, _folder, count in groups):
            self._add(self._every_path(changes))
        for text, folder, count in _grouped([(kept.path, kept.reason) for kept in plan.kept]):
            prefix = f"{count} kept" if count > 1 else "Kept"
            self._add(_row(f"{prefix}: {text}", f"In {folder}"))
        for note in plan.notes:
            self._add(_row(note))
        if plan.undo is not None and plan.undo.blocked:
            reason = plan.undo.blocked[:1].upper() + plan.undo.blocked[1:]
            self._add(
                _row(f"Undo isn't possible yet: {reason}.", f"Archive: {_home(plan.undo.archive)}")
            )
        elif plan.undo is not None:
            self._add(_row(f"Undo: {plan.undo.detail}", f"Archive: {_home(plan.undo.archive)}"))
        self.refresh_buttons()

    def _every_path(self, changes: Sequence[tuple[Path, str]]) -> Gtk.Widget:
        """The exact list, selectable, for a preview that grouped its rows."""
        label = Gtk.Label(
            label="\n".join(f"{path}  ({detail})" for path, detail in changes),
            xalign=0,
            selectable=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
        )
        label.add_css_class("caption")
        label.add_css_class("monospace")
        label.add_css_class("dim-label")
        row = Adw.PreferencesRow(activatable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        heading = Gtk.Label(label=f"Every path ({len(changes)})", xalign=0)
        heading.add_css_class("heading")
        box.append(heading)
        box.append(label)
        row.set_child(box)
        return row

    def _add(self, widget: Gtk.Widget) -> None:
        self.expander.add_row(widget)
        self.children.append(widget)

    def refresh_buttons(self) -> None:
        busy = self.section.busy
        self.apply.set_visible(bool(self.plan.changes))
        self.apply.set_sensitive(self.plan.ready and not busy)
        self.undo.set_visible(self.plan.undo is not None)
        self.undo.set_sensitive(not busy and not (self.plan.undo and self.plan.undo.blocked))
        self.keep.set_visible(self.plan.undo is not None and self.plan.undo.partial)
        self.keep.set_sensitive(not busy)
        self.retry.set_visible(bool(self.plan.retry))
        self.retry.set_label(self.plan.retry or "Retry")
        self.retry.set_sensitive(not busy)


class TidySection(Adw.PreferencesGroup):
    """Settings → Tidy up: the preview of every action, each with Apply and Undo."""

    def __init__(self, application: Application) -> None:
        super().__init__(
            title="Tidy up",
            description=(
                "Things older versions of Wall-in-One left behind. Nothing changes until "
                "you choose Apply: each change is listed here first, keeps a backup or "
                "goes into an archive, and can be undone."
            ),
        )
        self._app = application
        lane = _lane_of(application)
        self._own_lane = lane is None
        self._lane = lane if lane is not None else TidyLane()
        self._rows: dict[str, _ActionRow] = {}
        self._running: str | None = None
        self._planning = False
        self._reveal_pending = False
        self._replan = False
        self.status = _row("Checking…")
        self.add(self.status)
        self._check = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        self._check.set_tooltip_text("Check again")
        self._check.add_css_class("flat")
        self._check.connect("clicked", lambda _button: self.refresh())
        self.set_header_suffix(self._check)
        # Each visit shows what is there now; planning only reads.
        self.connect("map", lambda _widget: self.refresh())
        self.connect("unrealize", self._on_unrealize)

    @property
    def busy(self) -> bool:
        return self._running is not None or self._planning

    def row(self, action: str) -> _ActionRow | None:
        return self._rows.get(action)

    def refresh(self) -> None:
        """Plan again in the background and show the new preview."""
        if self.busy:
            # Asked while a preview or an action is in flight: look again
            # right after, so the newest state is what stays on screen.
            self._replan = True
            return
        self._planning = True
        self._check.set_sensitive(False)
        self._refresh_buttons()
        roots = _roots(self._app)
        if not self._lane.submit(lambda: tidy.plan(roots=roots), self._show_plan):
            self._planning = False
            self._check.set_sensitive(True)

    def _show_plan(self, plan: tidy.Plan | None, error: str) -> None:
        self._planning = False
        if self._replan:
            self._replan = False
            self.refresh()
            return
        self._check.set_sensitive(True)
        if plan is None:
            self.status.set_title("Tidy up couldn't check this profile")
            self.status.set_subtitle(error)
            self.status.set_visible(True)
            self._refresh_buttons()
            return
        self.status.set_visible(False)
        for action in plan.actions:
            existing = self._rows.get(action.action)
            if existing is None:
                existing = _ActionRow(self, action)
                self._rows[action.action] = existing
                self.add(existing.expander)
            else:
                existing.show(action)
        self._refresh_buttons()
        if self._reveal_pending:
            self._reveal_pending = False
            self.reveal()

    def _refresh_buttons(self) -> None:
        for row in self._rows.values():
            row.refresh_buttons()
            row.spinner.set_visible(self._running == row.plan.action)
            row.spinner.set_spinning(self._running == row.plan.action)

    def run(
        self, row: _ActionRow, *, undo: bool = False, retry: bool = False, keep: bool = False
    ) -> None:
        """Apply, undo, retry or keep one action, exactly as its preview showed it."""
        if self.busy:
            return
        ready = getattr(self._app, "require_authoring_ready", None)
        if callable(ready) and not ready():
            return
        plan = row.plan
        roots = _roots(self._app)

        def work() -> tidy.Result:
            if keep:
                return tidy.keep_archived(plan.action)
            if retry:
                return tidy.retry(plan.action)
            if undo:
                return tidy.undo(plan.action)
            return tidy.apply(plan.action, plan, roots=roots)

        self._running = plan.action
        self._refresh_buttons()
        if not self._lane.submit(work, self._finished):
            self._running = None
            self._refresh_buttons()

    def _finished(self, result: tidy.Result | None, error: str) -> None:
        self._running = None
        self._replan = False
        self._report(result.message if result is not None else error)
        self._refresh_buttons()
        self.refresh()

    def _report(self, message: str) -> None:
        report = getattr(self._app, "window_report", None)
        if callable(report) and message:
            report(message)

    def reveal(self) -> None:
        """Open the first action the card offered and bring it into view.

        Before the first preview has arrived, do it when it does.
        """
        if not self._rows or self._planning:
            self._reveal_pending = True
            return
        for row in self._rows.values():
            if row.plan.ready and row.plan.action in tidy.OFFERED:
                row.expander.set_expanded(True)
                row.expander.grab_focus()
                return
        self.grab_focus()

    def _on_unrealize(self, _widget: Gtk.Widget) -> None:
        if self._own_lane:
            self._lane.shutdown()


def _remember_dismissal() -> ui_prefs.UiPrefsDocument:
    try:
        return ui_prefs.update({"tidy_offer_dismissed": True})
    except ui_prefs.UiPrefsError as error:
        raise tidy.TidyError(str(error)) from error


class TidyCard(Gtk.Revealer):
    """The one-time offer after an update, shown only when there is something to tidy."""

    def __init__(self, application: Application, review: Callable[[], None]) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._app = application
        self._review = review
        self._started = False
        #: Set once the background check has answered (``plan`` stays None
        #: when the card was dismissed before, which skips planning).
        self.checked = False
        self.plan: tidy.Plan | None = None
        box = Gtk.Box(spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        box.add_css_class("card")
        inner = Gtk.Box(spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{side}")(12)
        icon = Gtk.Image(icon_name="edit-clear-all-symbolic", pixel_size=32)
        icon.set_valign(Gtk.Align.START)
        inner.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.title = Gtk.Label(label="Tidy up after the update", xalign=0, wrap=True)
        self.title.add_css_class("heading")
        self.body = Gtk.Label(xalign=0, wrap=True)
        text.append(self.title)
        text.append(self.body)
        inner.append(text)
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.not_now = Gtk.Button(label="Not Now")
        self.not_now.connect("clicked", lambda _button: self._dismiss(review=False))
        self.review = Gtk.Button(label="Review")
        self.review.add_css_class("suggested-action")
        self.review.connect("clicked", lambda _button: self._dismiss(review=True))
        buttons.append(self.not_now)
        buttons.append(self.review)
        inner.append(buttons)
        box.append(inner)
        self.set_child(box)
        self.set_reveal_child(False)

    def start(self) -> None:
        """Check once, in the background, whether there is anything to offer."""
        lane = _lane_of(self._app)
        if self._started or lane is None:
            return
        self._started = True
        roots = _roots(self._app)

        def check() -> tidy.Plan | None:
            if ui_prefs.load().prefs.tidy_offer_dismissed:
                return None
            return tidy.plan(roots=roots)

        lane.submit(check, self._checked)

    def _checked(self, plan: tidy.Plan | None, _error: str) -> None:
        self.checked = True
        self.plan = plan
        offered = plan.offer if plan is not None else ()
        if not offered:
            return
        titles = [action.title[:1].lower() + action.title[1:] for action in offered]
        listed = titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + " and " + titles[-1]
        self.body.set_text(
            "Older versions left a few things behind. Wall-in-One can "
            f"{listed}. Nothing changes until you review each one in Settings and choose "
            "Apply, and every change can be undone."
        )
        self.set_reveal_child(True)

    def _dismiss(self, *, review: bool) -> None:
        self.set_reveal_child(False)
        lane = _lane_of(self._app)
        if lane is not None:
            lane.submit(_remember_dismissal, self._remembered)
        if review:
            self._review()

    def _remembered(self, _document: Any, error: str) -> None:
        if not error:
            return
        report = getattr(self._app, "window_report", None)
        if callable(report):
            report(f"Wall-in-One couldn't remember hiding the Tidy up card: {error}")
