"""Settings → Tidy up and the one-time card, in the classic window.

The section is driven with a small application double over an isolated
profile. The card runs the real application on the golden profile, in
``tests/test_golden_tidy_card.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.shown_text import shown_texts  # noqa: E402
from tests.test_tidy import (  # noqa: E402
    _claim_folder,
    _noctalia_settings,
    kept_pairing_record,
)
from wall_in_one import paths, tidy  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.tidy_section import TidyLane, TidySection  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


_Result = TypeVar("_Result")

#: A wait that covers real storage (a survey of the leftovers, or an apply,
#: undo or retry with its moves, journal writes and fsyncs) waits for the
#: lane's worker on its own condition, not on GTK's two-second delivery bound
#: (``tests/test_ui_runtime.py`` splits its removal waits the same way).
REAL_IO_S = 30.0
#: GTK lays out and paints a window's first frame in one main-loop dispatch,
#: and nothing queued for the main loop is delivered until it returns. In a
#: fresh process that frame is the styles, the fonts and the first layout: it
#: measured 0.6 to 2.3 s on a loaded machine, and it is what the first wait of
#: the first test here timed out on, its survey long finished. It is GTK's own
#: work, not a delivery, so it gets this bound before any delivery is timed.
FIRST_FRAME_S = 30.0
#: A click runs one step on the worker and a survey after it, and a preview
#: asked for meanwhile adds one more. More rounds than this is not slowness.
_ROUNDS = 6


class _ObservedLane(TidyLane):
    """The section's lane, unchanged, with its worker's progress observable.

    Every real read, move, write and sync still happens; a wait only stops
    counting the worker's time as time GTK took to deliver a finished result.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._unfinished = 0

    @property
    def working(self) -> bool:
        """Whether the worker still has a step it was given to finish."""
        with self._lock:
            return self._unfinished > 0

    def _settle(self, by: int) -> None:
        with self._lock:
            self._unfinished += by

    def submit(
        self,
        work: Callable[[], _Result],
        done: Callable[[_Result | None, str], None],
    ) -> bool:
        def observed() -> _Result:
            try:
                return work()
            finally:
                self._settle(-1)

        self._settle(1)
        accepted = super().submit(observed, done)
        if not accepted:
            self._settle(-1)
        return accepted


class _Application:
    """What the section asks of the application, and nothing else."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(roots=())
        self.session = SimpleNamespace(library=SimpleNamespace(items=()))
        self.tidy_lane = _ObservedLane()
        self.reports: list[str] = []
        self.ready = True
        #: Set by the window's frame clock once its first frame is painted.
        self.first_frame = False

    def painted(self, _clock: object) -> None:
        self.first_frame = True

    def window_report(self, message: str) -> None:
        self.reports.append(message)

    def require_authoring_ready(self) -> bool:
        return self.ready


@pytest.fixture
def section(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TidySection, _Application]]:
    reloads: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: reloads.append("config-reload"))
    application = _Application()
    page = Adw.PreferencesPage()
    group = TidySection(application)  # type: ignore[arg-type]
    page.add(group)
    window = Gtk.Window(child=page, default_width=900, default_height=900)
    window.present()
    clock = window.get_frame_clock()
    assert clock is not None
    clock.connect("after-paint", application.painted)
    try:
        yield group, application
    finally:
        application.tidy_lane.shutdown()
        window.destroy()


def _busy(group: TidySection) -> bool:
    return group.busy


def _tidied(application: _Application, condition: Callable[[], object], what: str) -> None:
    """Wait for Tidy up's worker, then for GTK to deliver, until ``condition`` holds.

    The window's first frame comes first (`FIRST_FRAME_S`). Then each step on
    the worker gets the storage bound (`REAL_IO_S`), and each delivery on GTK
    after it keeps `spin_until`'s two-second bound. A delivery can start the
    next step (the survey after an apply), so those two alternate.
    """
    spin_until(
        lambda: application.first_frame,
        FIRST_FRAME_S,
        what="the window's first frame (GTK's own first layout)",
    )
    lane = application.tidy_lane
    for _round in range(_ROUNDS):
        spin_until(
            lambda: not lane.working, REAL_IO_S, what=f"Tidy up's worker (real storage): {what}"
        )
        spin_until(lambda: lane.working or bool(condition()), what=f"{what}, delivered on GTK")
        if condition():
            return
    raise AssertionError(f"Tidy up's worker was still being given work: {what}")


def _idle(group: TidySection) -> bool:
    return not group.busy and group.row(tidy.LEFTOVERS) is not None


def _texts(group: TidySection, action: str) -> list[str]:
    row = group.row(action)
    assert row is not None
    found: list[str] = []
    for child in row.children:
        if isinstance(child, Adw.ActionRow):
            found.append(child.get_title())
        else:
            found.extend(label.get_text() for label in _labels(child))
    return found


def _labels(widget: Gtk.Widget) -> Iterator[Gtk.Label]:
    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _labels(child)
        child = child.get_next_sibling()


def test_the_section_previews_applies_and_undoes_each_action(
    section: tuple[TidySection, _Application],
) -> None:
    group, application = section
    settings = _noctalia_settings()
    original = settings.read_bytes()
    for name in ("entry-aaaaaaaa", "entry-bbbbbbbb", "entry-cccccccc"):
        _claim_folder(paths.app_state_dir(), name)
    group.refresh()
    _tidied(
        application,
        lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None,
        "the first survey",
    )

    leftovers = group.row(tidy.LEFTOVERS)
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    old_template = group.row(tidy.OLD_PALETTE_TEMPLATE)
    cache = group.row(tidy.THUMBNAIL_CACHE)
    assert leftovers and plugin and old_template and cache
    assert "3 items: empty claim folder from a finished removal" in _texts(group, tidy.LEFTOVERS)
    assert any(text.startswith("Every path (3)") for text in _texts(group, tidy.LEFTOVERS))
    plugin_texts = _texts(group, tidy.PLUGIN_SETTINGS)
    assert any("remove hub_placement" in text for text in plugin_texts)
    # "<date>-<time>" in the backup note: shown as written, not as failed markup.
    assert any("bak-wall-in-one-<date>-<time>" in text for text in shown_texts(plugin.expander))
    assert any(text.startswith("Kept: refresh_interval_seconds") for text in plugin_texts)
    assert plugin.apply.get_visible() and plugin.apply.get_sensitive()
    assert not plugin.undo.get_visible()
    assert not old_template.apply.get_visible(), "nothing to archive while Noctalia uses it"
    assert not cache.apply.get_visible() and cache.apply.get_label() == "Clear Cache"

    application.ready = False
    plugin.apply.emit("clicked")
    assert not _busy(group), "nothing starts while authoring is paused"
    application.ready = True

    plugin.apply.emit("clicked")
    assert _busy(group) and plugin.spinner.get_visible() and not leftovers.apply.get_sensitive()
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.undo is not None,
        "the apply, then its survey",
    )

    assert application.reports[-1].startswith("Removed 3 old plugin settings")
    assert "hub_placement" not in settings.read_text()
    assert plugin.undo.get_visible() and not plugin.apply.get_visible()
    assert not plugin.spinner.get_visible()

    plugin.undo.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.undo is None,
        "the undo, then its survey",
    )

    assert settings.read_bytes() == original
    assert application.reports[-1] == "Noctalia's settings are back as they were."

    leftovers.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is not None,
        "the apply, then its survey",
    )
    assert not any((paths.app_state_dir() / ".wall-in-one-retained").iterdir())
    leftovers.undo.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is None,
        "the undo, then its survey",
    )
    assert len(list((paths.app_state_dir() / ".wall-in-one-retained").iterdir())) == 3


def test_the_section_lists_a_copy_kept_beside_a_library_video(
    section: tuple[TidySection, _Application],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AB-7, as the window shows it: the folders of the library's items come from
    the last scan, and a copy kept beside a video in one of them is a "Kept" row."""
    group, application = section
    root = tmp_path / "library"
    video = root / "trips" / "2025" / "clip.mp4"
    kept_pairing_record(video, monkeypatch)
    application.settings = SimpleNamespace(roots=(root,))
    application.session = SimpleNamespace(
        library=SimpleNamespace(items=(SimpleNamespace(path=video),))
    )

    group.refresh()
    _tidied(application, lambda: _idle(group), "the first survey")

    texts = _texts(group, tidy.LEFTOVERS)
    # Failed on 267c301: no row for it at all.
    assert any(
        text.startswith("Kept: a copy Wall-in-One kept instead of deleting it") for text in texts
    ), texts
    leftovers = group.row(tidy.LEFTOVERS)
    assert leftovers is not None and not leftovers.plan.changes, "and nothing is offered"


def test_a_preview_gone_stale_is_refused_and_replaced(
    section: tuple[TidySection, _Application],
) -> None:
    group, application = section
    settings = _noctalia_settings()
    group.refresh()
    _tidied(
        application,
        lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None,
        "the first survey",
    )
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    assert plugin is not None
    shown = plugin.plan
    settings.write_text(settings.read_text().replace('mode = "dark"', 'mode = "light"'))
    saved = settings.read_bytes()

    plugin.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.token != shown.token,
        "the refused apply, then its survey",
    )

    assert "changed since the preview" in application.reports[-1]
    assert settings.read_bytes() == saved
    assert plugin.plan.ready, "the new preview can be applied"


def test_an_unconfirmed_reload_offers_retry_until_noctalia_answers(
    section: tuple[TidySection, _Application], monkeypatch: pytest.MonkeyPatch
) -> None:
    group, application = section
    _noctalia_settings()
    answers = [False]
    monkeypatch.setattr(tidy, "_reload_noctalia", lambda: answers[0])
    group.refresh()
    _tidied(
        application,
        lambda: _idle(group) and group.row(tidy.PALETTE_TEMPLATE) is not None,
        "the first survey",
    )
    switch = group.row(tidy.PALETTE_TEMPLATE)
    assert switch is not None and not switch.retry.get_visible()

    switch.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and switch.plan.retry != "",
        "the apply, then its survey",
    )

    assert "didn't confirm" in application.reports[-1]
    assert switch.retry.get_visible() and switch.retry.get_label() == "Retry Reload"
    answers[0] = True
    switch.retry.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and switch.plan.retry == "",
        "the retried reload, then its survey",
    )

    assert application.reports[-1] == "Noctalia reloaded its settings."
    assert not switch.retry.get_visible()


def test_an_undo_left_unfinished_offers_undo_again_or_keep_archived(
    section: tuple[TidySection, _Application],
) -> None:
    group, application = section
    folder = _claim_folder(paths.app_state_dir(), "entry-dddddddd")
    group.refresh()
    _tidied(
        application, lambda: _idle(group) and bool(group.row(tidy.LEFTOVERS)), "the first survey"
    )
    leftovers = group.row(tidy.LEFTOVERS)
    assert leftovers is not None and leftovers.plan.ready
    leftovers.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is not None,
        "the apply, then its survey",
    )
    folder.mkdir(mode=0o700)  # something new takes the archived folder's name

    leftovers.undo.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is not None and leftovers.plan.undo.partial,
        "the undo that leaves an item archived, then its survey",
    )

    assert "can be tried again" in application.reports[-1]
    assert leftovers.undo.get_visible() and leftovers.keep.get_visible()
    leftovers.keep.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is None,
        "Keep Archived, then its survey",
    )
    assert not leftovers.keep.get_visible() and "no longer offered" in application.reports[-1]


def test_undo_is_shown_withheld_with_its_reason_while_its_archive_cannot_be_read(
    section: tuple[TidySection, _Application],
) -> None:
    """A2, as the window shows it: the archive can't be read, so Undo is there,
    can't be chosen, and its row says which archive and why."""
    group, application = section
    _claim_folder(paths.app_state_dir(), "entry-eeeeeeee")
    group.refresh()
    _tidied(
        application, lambda: _idle(group) and bool(group.row(tidy.LEFTOVERS)), "the first survey"
    )
    leftovers = group.row(tidy.LEFTOVERS)
    assert leftovers is not None and leftovers.plan.ready
    leftovers.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and leftovers.plan.undo is not None,
        "the apply, then its survey",
    )
    assert leftovers.plan.undo is not None and leftovers.undo.get_sensitive()
    archive = leftovers.plan.undo.archive

    archive.chmod(0o600)  # there, and no longer searchable
    try:
        group.refresh()
        _tidied(
            application,
            lambda: (
                _idle(group)
                and leftovers.plan.undo is not None
                and bool(leftovers.plan.undo.blocked)
            ),
            "the survey of an archive that can't be read",
        )
    finally:
        archive.chmod(0o700)

    # Failed on 2b8e720: no Undo button and no row, as if nothing had been archived.
    assert leftovers.undo.get_visible() and not leftovers.undo.get_sensitive()
    assert not leftovers.keep.get_visible()
    (row,) = [text for text in _texts(group, tidy.LEFTOVERS) if text.startswith("Undo")]
    assert row.startswith("Undo isn't possible yet: The manifest of the archive "), row
    assert row.endswith(
        "can't be read (Permission denied), and it may hold a later change; Undo waits "
        "until it can be read."
    ), row


def test_apply_is_unavailable_with_its_reason_while_an_undo_waits_for_its_reload(
    section: tuple[TidySection, _Application], monkeypatch: pytest.MonkeyPatch
) -> None:
    group, application = section
    _noctalia_settings()
    group.refresh()
    _tidied(
        application,
        lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None,
        "the first survey",
    )
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    assert plugin is not None and plugin.plan.ready
    plugin.apply.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.undo is not None,
        "the apply, then its survey",
    )
    monkeypatch.setattr(tidy, "_reload_noctalia", lambda: False)

    plugin.undo.emit("clicked")
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.retry != "",
        "the undo that waits for its reload, then its survey",
    )

    assert plugin.apply.get_visible() and not plugin.apply.get_sensitive()
    assert any(text.startswith("Not now") for text in _texts(group, tidy.PLUGIN_SETTINGS))
    assert "Retry Reload first" in plugin.plan.blocked
    assert plugin.retry.get_visible()


def test_a_report_that_raised_gives_the_buttons_back(
    section: tuple[TidySection, _Application], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AA-3's sibling in Tidy up (both windows): the report came before the buttons
    and the next preview, so a report that raised left them as if still running."""
    group, application = section
    _noctalia_settings()
    group.refresh()
    _tidied(
        application,
        lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None,
        "the first survey",
    )
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    assert plugin is not None
    real_report = application.window_report
    raised: list[str] = []

    def report_raises_once(message: str) -> None:
        if not raised:
            raised.append(message)
            raise RuntimeError("injected: the report failed")
        real_report(message)

    monkeypatch.setattr(application, "window_report", report_raises_once)
    plugin.apply.emit("clicked")
    _tidied(application, lambda: not group.busy and bool(raised), "the apply to finish")
    # Before: the spinner kept spinning and Undo never came, until something else
    # refreshed the section.
    _tidied(
        application,
        lambda: _idle(group) and plugin.plan.undo is not None,
        "the preview after the apply",
    )
    assert not plugin.spinner.get_visible()
    assert plugin.undo.get_visible() and plugin.undo.get_sensitive()

    plugin.undo.emit("clicked")
    _tidied(application, lambda: _idle(group) and plugin.plan.undo is None, "the undo")
