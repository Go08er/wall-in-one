"""A number's settle timer must not write after "Use defaults" or once its pane moved on.

dial_rows.own_row hands a spin's value on once it has been still for SETTLE_MS (450 ms).
A person who nudges the speed and then presses "Use defaults" within that time sees the
editor go back to the defaults, then the stale timer writes the speed again.
Desired: after "Use defaults", the wallpaper has no override of its own.
Control: the same with "Use defaults" pressed after the timer fired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

import wall_in_one.ui.pairings_page as pairings_page  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_pairing_picker import (  # noqa: E402
    PairingApp,
    QuietPreviewLoader,
    QuietThumbnailLoader,
)
from tests.test_ui_renderer_dials import _reset_of, toolkit  # noqa: E402,F401
from wall_in_one import config  # noqa: E402
from wall_in_one.library import pairings  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import dial_rows  # noqa: E402


def _drain(milliseconds: int) -> None:
    done: list[bool] = []

    def finish() -> bool:
        done.append(True)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(milliseconds, finish)
    spin_until(lambda: bool(done), 5, what="the main loop to run")


@pytest.mark.parametrize("wait_first", [False, True], ids=["reset-within-settle", "control"])
def test_use_defaults_is_the_last_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_first: bool
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    library = Library(roots=(tmp_path,), items=(video,))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    session.pairings.set_dial(video, "video", "deband", True)  # so "Use defaults" is live
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, video)

    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    if wait_first:
        _drain(dial_rows.SETTLE_MS + 300)
    assert page.renderer_reset is not None and page.renderer_reset.get_sensitive()
    page.renderer_reset.emit("clicked")
    _drain(dial_rows.SETTLE_MS + 300)

    record = session.pairings.get(pairings.Identity.of(video))
    own = record.renderer if record is not None else pairings.RendererOverrides()
    root.destroy()
    assert not own, f"after Use defaults the wallpaper still has its own: {own}"


def test_a_number_nudged_on_one_wallpaper_is_saved_for_it_and_not_for_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    first = MediaItem(tmp_path / "a.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "a.png")
    second = MediaItem(tmp_path / "b.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "b.png")
    library = Library(roots=(tmp_path,), items=(first, second))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, first)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    page.edit(session, second)
    _drain(dial_rows.SETTLE_MS + 300)
    owns = [
        getattr(session.pairings.get(pairings.Identity.of(item)), "renderer", None)
        for item in (first, second)
    ]
    root.destroy()
    # Flushed to the wallpaper it was entered for; never to the one shown now.
    assert owns[0] is not None and owns[0].dial("video", "speed") == 1.5, owns
    assert not owns[1], f"written to the wallpaper the pane moved on to: {owns}"


@pytest.mark.parametrize(
    "gap_ms", [300, 700], ids=["second-nudge-inside-first-settle", "control-after"]
)
def test_two_numbers_nudged_close_together_are_both_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gap_ms: int
) -> None:
    """Speed's save rebuilds the pane; brightness, nudged meanwhile, is saved too."""
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    library = Library(roots=(tmp_path,), items=(video,))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    _drain(gap_ms)
    brightness = page.renderer_rows["dial:video:brightness"]
    assert isinstance(brightness, Adw.SpinRow)
    brightness.set_value(20)
    _drain(1500)
    record = session.pairings.get(pairings.Identity.of(video))
    own = dict(record.renderer.video) if record is not None else {}
    root.destroy()
    assert own.get("speed") == 1.5 and own.get("brightness") == 20, f"saved: {own}"


def test_use_defaults_still_wins_over_every_pending_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    library = Library(roots=(tmp_path,), items=(video,))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    session.pairings.set_dial(video, "video", "deband", True)
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, video)
    for name, value in (("speed", 1.5), ("brightness", 20)):
        row = page.renderer_rows[f"dial:video:{name}"]
        assert isinstance(row, Adw.SpinRow)
        row.set_value(value)
    assert page.renderer_reset is not None
    page.renderer_reset.emit("clicked")
    _drain(dial_rows.SETTLE_MS + 600)
    record = session.pairings.get(pairings.Identity.of(video))
    own = record.renderer if record is not None else pairings.RendererOverrides()
    root.destroy()
    assert not own, f"a pending number outlived Use defaults: {own}"


def test_a_number_nudged_just_before_the_window_goes_is_saved_for_its_wallpaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    library = Library(roots=(tmp_path,), items=(video,))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    _drain(100)
    root.destroy()
    _drain(dial_rows.SETTLE_MS + 600)
    record = session.pairings.get(pairings.Identity.of(video))
    assert record is not None and record.renderer.dial("video", "speed") == 1.5


class GuardedApp(PairingApp):
    """PairingApp whose prepare refuses a removed item, as app.py:3997-4006 does."""

    def prepare_pairing_mutation(self, item: MediaItem, mutation: Any) -> Any:
        current = self.session.library.find(item.path)
        if current is None or pairings.Identity.of(current) != pairings.Identity.of(item):
            raise ValueError(f"{item.name} was removed before the authoring change could save")
        return lambda: mutation(self.session.pairings, current)


def _open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, app: type[PairingApp] = PairingApp
) -> tuple[Session, PairingApp, pairings_page.PairingsPage, Gtk.Window, MediaItem, list[Library]]:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    holder = [Library(roots=(tmp_path,), items=(video,))]
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: holder[0],
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    application = app(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    return session, application, page, root, video, holder


def _own(session: Session, item: MediaItem) -> pairings.RendererOverrides:
    record = session.pairings.get(pairings.Identity.of(item))
    return record.renderer if record is not None else pairings.RendererOverrides()


# -- a reset clicked after a rebuild, before the flush idle has run (L7) -----------------


@pytest.mark.parametrize("idle_first", [False, True], ids=["reset-before-idle", "control"])
def test_use_defaults_after_a_rebuild_is_the_last_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idle_first: bool
) -> None:
    session, _app, page, root, video, _holder = _open(tmp_path, monkeypatch)
    session.pairings.set_dial(video, "video", "brightness", 10)  # so Use defaults is live
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)  # pending on the first build's Settling
    deband = page.renderer_rows["dial:video:deband"]
    assert isinstance(deband, Adw.SwitchRow)
    deband.set_active(True)  # immediate save -> rebuild -> old Settling.flush_soon (idle)
    assert page.renderer_rows["dial:video:deband"] is not deband, "the pane was not rebuilt"
    if idle_first:
        _drain(30)
    assert page.renderer_reset is not None and page.renderer_reset.get_sensitive()
    page.renderer_reset.emit("clicked")  # the rebuilt pane's Use defaults
    _drain(dial_rows.SETTLE_MS + 600)
    own = _own(session, video)
    root.destroy()
    assert not own, f"after Use defaults the wallpaper still has its own: {own}"


@pytest.mark.parametrize("idle_first", [False, True], ids=["reset-before-idle", "control"])
def test_a_dials_reset_after_a_rebuild_is_the_last_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, idle_first: bool
) -> None:
    session, _app, page, root, video, _holder = _open(tmp_path, monkeypatch)
    session.pairings.set_dial(video, "video", "speed", 1.25)  # so its reset is shown
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    deband = page.renderer_rows["dial:video:deband"]
    assert isinstance(deband, Adw.SwitchRow)
    deband.set_active(True)
    if idle_first:
        _drain(30)
    rebuilt = page.renderer_rows["dial:video:speed"]
    assert rebuilt is not speed
    reset = _reset_of(rebuilt)
    assert reset.get_visible()
    reset.emit("clicked")
    _drain(dial_rows.SETTLE_MS + 600)
    own = _own(session, video)
    root.destroy()
    assert own.dial("video", "speed") is None, f"speed after its own reset: {own}"


# -- the wallpaper leaves the library while its number is waiting (N7) ---------------------


def test_a_removed_wallpaper_gets_no_record_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _application, page, root, video, holder = _open(tmp_path, monkeypatch, app=GuardedApp)
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    holder[0] = Library(roots=(tmp_path,), items=())
    session.refresh()
    page.refresh(session)  # no _build_renderer, so no flush; the timer lives
    _drain(dial_rows.SETTLE_MS + 600)
    record = session.pairings.get(pairings.Identity.of(video))
    root.destroy()
    assert record is None, f"a record came back for a removed wallpaper: {record}"


def test_a_removed_wallpaper_is_not_reported_as_a_failed_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, application, page, root, video, holder = _open(tmp_path, monkeypatch, app=GuardedApp)
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    speed.set_value(1.5)
    holder[0] = Library(roots=(tmp_path,), items=())
    session.refresh()
    page.refresh(session)
    _drain(dial_rows.SETTLE_MS + 600)
    messages = list(application.messages)
    root.destroy()
    assert not messages, f"reported after the wallpaper was removed: {messages}"


def test_a_reset_of_one_wallpaper_leaves_the_pending_number_of_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    first = MediaItem(tmp_path / "a.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "a.png")
    second = MediaItem(tmp_path / "b.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "b.png")
    library = Library(roots=(tmp_path,), items=(first, second))
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    session.pairings.set_dial(second, "video", "deband", True)  # so its Use defaults is live
    page = pairings_page.PairingsPage(cast(Any, PairingApp(session)), lambda: None)
    root = Gtk.Window()
    root.set_child(page)
    page.edit(session, first)
    row = page.renderer_rows["dial:video:speed"]
    assert isinstance(row, Adw.SpinRow)
    row.set_value(1.5)  # pending for a
    page.edit(session, second)  # a's number is handed to an idle
    assert page.renderer_reset is not None and page.renderer_reset.get_sensitive()
    page.renderer_reset.emit("clicked")  # b's Use defaults, before the idle
    _drain(dial_rows.SETTLE_MS + 600)
    own_a, own_b = _own(session, first), _own(session, second)
    root.destroy()
    assert own_a.dial("video", "speed") == 1.5, (own_a, own_b)
    assert not own_b, own_b
