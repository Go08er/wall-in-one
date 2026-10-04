"""``--ui=next``'s Library edits, driven through the inspector against the real application.

Each test runs `Application.run` with the new window, as `test_ui_next_slice`
does: a sandboxed profile with two wallpapers (``dawn``, ``dusk``), a fake
runtime, no Noctalia, a non-unique GApplication and a deadline on every wait.
The writes go through the application's real authoring lane into the real
stores; a test that wants gestures to queue holds the lane with a task of its
own until it lets go.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from tests.golden import harness  # noqa: E402
from tests.test_next_status_line import two_display_status  # noqa: E402
from tests.test_ui_next_slice import (  # noqa: E402
    finish,
    library_page,
    run,
    settled_after_a_refused_publication,
)
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    sandboxed_runtime,
    settled,
)
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.library import favourites, pairings, playlists  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.inspector import Inspector  # noqa: E402
from wall_in_one.ui.next.library import LibraryPage  # noqa: E402
from wall_in_one.ui.next.real_editing import RealEditing  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402
from wall_in_one.ui.next.state import Removal  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def wallpaper(tmp_path: Path, name: str) -> str:
    """A library wallpaper's id: its path (``dawn.png`` or ``dusk.jpg``)."""
    return str(tmp_path / "wallpapers" / name)


def item_at(application: Application, wid: str) -> MediaItem:
    found = application.session.library.find(Path(wid))
    assert found is not None
    return found


def durable(item: MediaItem) -> pairings.PalettePolicy:
    record = pairings.Store.open().get(pairings.Identity.of(item))
    return record.palette if record is not None and record.customized else pairings.PalettePolicy()


def reports_of(window: NextWindow, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every transient message the window shows, as the application reports it."""
    said: list[str] = []
    shown = window.report

    def report(message: str) -> None:
        said.append(message)
        shown(message)

    monkeypatch.setattr(window, "report", report)
    return said


def hold_the_lane(application: Application) -> threading.Event:
    """Keep the authoring lane busy until the returned event is set (bounded)."""
    gate = threading.Event()
    assert application.authoring_action_async(lambda: gate.wait(10), lambda _done: None)
    return gate


def every_target(model: Gio.MenuModel | None) -> list[str]:
    """Every item's action target in ``model``, sections and submenus included."""
    found: list[str] = []
    if model is None:
        return found
    for index in range(model.get_n_items()):
        for link in (Gio.MENU_LINK_SECTION, Gio.MENU_LINK_SUBMENU):
            linked = model.get_item_link(index, link)
            if linked is not None:
                found.extend(every_target(linked))
        target = model.get_item_attribute_value(index, Gio.MENU_ATTRIBUTE_TARGET, None)
        if target is not None:
            found.append(target.get_string())
    return found


def inspected(page: LibraryPage) -> str | None:
    """The wallpaper whose details are open, read afresh each time."""
    return page._inspected


def inspector_of(window: NextWindow) -> Inspector:
    return library_page(window).inspector


def first_scan(application: Application, window: NextWindow) -> Step:
    return (
        "the first scan",
        lambda: settled(application) and window.library_text == "2 wallpapers in the library",
    )


def test_inspector_colors_queue_settle_and_fall_back_to_what_was_saved(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    dusk = wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        page = library_page(window)
        page.inspect(window.state.wallpaper(dusk))
        inspector = inspector_of(window)
        assert inspector.color_mode is not None and inspector.theme_mode is not None
        assert inspector.color_mode.get_active_name() == "adaptive"
        assert inspector.reset_button is None, "nothing chosen yet: no reset"
        assert inspector.retry_button is None
        item = item_at(application, dusk)

        # Two gestures while the lane is busy: each queues, the view shows the latest.
        before = window.state.wallpaper(dusk)  # what a card built before them holds
        gate = hold_the_lane(application)
        inspector.theme_mode.set_active_name("dark")
        inspector.color_mode.set_active_name("keep")
        assert len(application._authoring_queue) == 2
        view = window.state.wallpaper(dusk)
        assert (view.color_mode, view.theme_mode) == ("keep", "dark")
        assert durable(item) == pairings.PalettePolicy(), "nothing saved yet"

        # Once the last save lands the pane shows what is saved at once, Reset
        # included: no library rescan has to come first, so none is let run,
        # and nothing redraws the pane while the saves wait.
        rescans: list[int] = []
        rescan = application.refresh_library
        held_rescans = True

        def refresh_library() -> None:
            if held_rescans:
                rescans.append(1)
            else:
                rescan()

        monkeypatch.setattr(application, "refresh_library", refresh_library)
        gate.set()
        yield "both saves", lambda: settled(application)
        assert durable(item) == pairings.PalettePolicy("keep", "", pairings.Mode.DARK)
        inspector = inspector_of(window)
        assert inspector.color_mode is not None and inspector.theme_mode is not None
        assert inspector.color_mode.get_active_name() == "keep"
        assert inspector.theme_mode.get_active_name() == "dark"
        assert inspector.reset_button is not None, "now there is something to reset"
        held_rescans = False
        for _held in rescans:
            rescan()
        yield "the held rescans", lambda: settled(application)

        # A card's view from before the change opens what is saved now.
        page.inspect(before)
        inspector = inspector_of(window)
        assert inspector.color_mode is not None and inspector.theme_mode is not None
        assert inspector.color_mode.get_active_name() == "keep"
        assert inspector.theme_mode.get_active_name() == "dark"

        # A save that fails puts the control back on what pairings.json holds.
        original = pairings.Store.choose_mode

        def flaky(
            store: pairings.Store, current: MediaItem, mode: pairings.Mode
        ) -> pairings.Pairing:
            if mode is pairings.Mode.LIGHT:
                raise pairings.PairingError("local-io", "disk full")
            return original(store, current, mode)

        monkeypatch.setattr(pairings.Store, "choose_mode", flaky)
        gate = hold_the_lane(application)
        inspector.theme_mode.set_active_name("light")
        # Anything that redraws the pane while it waits (Noctalia's colors
        # changed, a playlist, what is on screen) shows the queued choice,
        # never the view the pane was last built from.
        state = window.state
        assert isinstance(state, RealAppState)
        state.emit_changed("theme")
        inspector = inspector_of(window)
        assert inspector.color_mode is not None and inspector.theme_mode is not None
        assert inspector.theme_mode.get_active_name() == "light"
        assert inspector.color_mode.get_active_name() == "keep"
        gate.set()
        yield "the failed save", lambda: settled(application) and bool(said)
        assert said[-1] == "Colors for dusk were not saved; nothing changed: local-io: disk full"
        inspector = inspector_of(window)
        assert inspector.theme_mode is not None
        assert inspector.theme_mode.get_active_name() == "dark", "rebuilt from durable"
        assert durable(item).mode is pairings.Mode.DARK

        # "Palette" with none chosen lists them and writes nothing.
        assert inspector.color_mode is not None
        inspector.color_mode.set_active_name("palette")
        assert not application._authoring_queue and not application._authoring_active
        assert window.state.wallpaper(dusk).color_mode == "keep"
        assert inspector.color_detail is not None
        assert inspector.color_detail.get_first_child() is not None, "the palette list"

        # Reset to defaults forgets it all.
        assert inspector.reset_button is not None
        inspector.reset_button.emit("clicked")
        yield (
            "the reset",
            lambda: settled(application) and said[-1] == "dusk is back to its defaults",
        )
        assert durable(item) == pairings.PalettePolicy()
        assert window.state.wallpaper(dusk).color_mode == "adaptive"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_a_skipped_wallpaper_cannot_be_applied_until_try_again(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dawn = wallpaper(tmp_path, "dawn.png")
    path = Path(dawn)
    pairings.Store.open().mark_borked(
        MediaItem(path=path, kind=Kind.STILL, size=path.stat().st_size, mtime=0),
        "the renderer crashed twice",
        "renderer-crash",
    )
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        page = library_page(window)
        card = page._cards[dawn]
        assert card.apply_button is not None and not card.apply_button.get_sensitive()
        assert page._notice.get_revealed(), "the Library says one is being skipped"
        page.inspect(window.state.wallpaper(dawn))
        inspector = inspector_of(window)
        apply_button = inspector.apply_button
        assert apply_button is not None and not apply_button.get_sensitive()
        assert "the renderer crashed twice" in (apply_button.get_tooltip_text() or "")
        assert inspector.retry_button is not None and inspector.retry_button.get_sensitive()
        inspector.retry_button.emit("clicked")
        yield "the warning to be cleared", lambda: settled(application) and bool(said)
        assert said == ["dawn will be tried again"]
        assert window.state.wallpaper(dawn).problem == ""
        record = pairings.Store.open().get(pairings.Identity.of(item_at(application, dawn)))
        assert record is None or not record.health.is_borked
        apply_button = inspector_of(window).apply_button
        assert apply_button is not None and apply_button.get_sensitive()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_removal_is_by_ownership_and_a_users_file_goes_to_the_trash(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    dawn, dusk = wallpaper(tmp_path, "dawn.png"), wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        page = library_page(window)

        # A download Wall-in-One owns is deleted, and only after a question.
        monkeypatch.setattr(
            RealEditing,
            "removal",
            lambda _editing, wid: Removal("delete") if wid == dusk else Removal("trash"),
        )
        page.inspect(window.state.wallpaper(dusk))
        remove = inspector_of(window).remove_button
        assert remove is not None and remove.get_label() == "Delete from disk\u2026"
        remove.emit("clicked")
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog)
        dialog.emit("response", "cancel")
        dialog.force_close()
        assert not application._authoring_queue and Path(dusk).exists()

        # Forged as a download, the user's file is still never deleted: the
        # removal itself re-derives ownership from the disk and refuses.
        page.inspect(window.state.wallpaper(dusk))
        remove = inspector_of(window).remove_button
        assert remove is not None
        remove.emit("clicked")
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog)
        dialog.emit("response", "delete")
        dialog.force_close()
        yield "the refused deletion", lambda: settled(application) and bool(said)
        assert said[-1].startswith("Nothing was deleted"), said
        assert Path(dusk).exists()
        assert inspected(page) == dusk, "a refused removal leaves its details open"

        # The user's own file goes to the Trash at once: no question, no Undo.
        page.inspect(window.state.wallpaper(dawn))
        remove = inspector_of(window).remove_button
        assert remove is not None and remove.get_label() == "Move to Trash"
        remove.emit("clicked")
        assert window.get_visible_dialog() is None
        yield (
            "the trashed file to leave the library",
            lambda: settled(application) and window.library_text == "1 wallpaper in the library",
        )
        assert said[-1] == "dawn moved to the Trash"
        assert inspected(page) is None, "its details closed once it left the library"
        assert not Path(dawn).exists()
        assert (paths.data_home() / "Trash" / "files" / "dawn.png").exists()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_playlists_from_the_card_menu_one_task_never_your_pick(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dawn, dusk = wallpaper(tmp_path, "dawn.png"), wallpaper(tmp_path, "dusk.jpg")
    store = playlists.Store.open()
    frog = store.create("Frog day")
    store.set_singleton(QUICK_CHOICE_ID, "Quick choice", Path(dawn))
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        toasts: list[tuple[str, object]] = []
        window.state.connect("toast", lambda _state, text, button: toasts.append((text, button)))
        yield first_scan(application, window)
        page = library_page(window)
        targets = every_target(page._card_menu_model(window.state.wallpaper(dusk)))
        assert f"{dusk}|{frog.id}" in targets
        assert not any(QUICK_CHOICE_ID in target for target in targets), "your pick is hidden"
        assert not any("all-media" in target for target in targets)

        page._action_group.activate_action("add", GLib.Variant("s", f"{dusk}|{frog.id}"))
        yield "the add", lambda: settled(application) and bool(toasts)
        saved = playlists.Store.open().get(frog.id)
        assert saved is not None and [str(entry.path) for entry in saved.entries] == [dusk]
        assert toasts[-1][0] == "Added \u201cdusk\u201d to \u201cFrog day\u201d"
        assert window.state.playlist(frog.id).entries == (dusk,)

        # A card can be dragged: its id, as the sidebar and the Playlists page take it.
        card = page._cards[dusk]
        controllers = card.frame.observe_controllers()
        assert any(
            isinstance(controllers.get_item(i), Gtk.DragSource)
            for i in range(controllers.get_n_items())
        )

        # New playlist..., made with the wallpaper in it, in one save.
        page._action_group.activate_action("new-playlist", GLib.Variant("s", dawn))
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog)
        entry = dialog.get_extra_child()
        assert isinstance(entry, Gtk.Entry)
        entry.set_text("Evening")
        dialog.emit("response", "create")
        dialog.force_close()
        yield "the new playlist", lambda: settled(application) and len(toasts) == 2
        assert toasts[-1][0] == "Created \u201cEvening\u201d with \u201cdawn\u201d"
        made = playlists.Store.open().by_name("Evening")
        assert made is not None and [str(entry.path) for entry in made.entries] == [dawn]
        assert f"playlist:{made.id}" in window.nav_keys
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_bulk_favorite_and_one_removal_at_a_time(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    dawn, dusk = wallpaper(tmp_path, "dawn.png"), wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        page = library_page(window)
        page._select.set_active(True)
        page._toggle_selected(dawn)
        page._toggle_selected(dusk)
        assert not page._remove_all.get_sensitive()
        assert page._remove_all.get_tooltip_text() == "Remove one wallpaper at a time"
        assert page._fav_all.get_sensitive()
        page._bulk_favorite()
        assert len(application._authoring_queue) + int(application._authoring_active) == 1
        yield "the favorites", lambda: settled(application) and bool(said)
        assert said == ["Added 2 wallpapers to favorites"]
        assert favourites.Store.open().paths == {Path(dawn), Path(dusk)}
        assert all(view.favorite for view in window.state.wallpapers)
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_add_a_folder_saves_it_then_scans_it(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "more walls"
    extra.mkdir()
    (extra / "noon.png").write_bytes(harness.tiny_png((200, 120, 40), size=8))
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        page = library_page(window)
        assert page.header_start() == [page._add] and page._add.get_sensitive()
        first = config.load().roots
        page.add_folder(str(extra))
        assert said == [], "nothing is said before settings.toml has it"
        yield (
            "the folder to be saved and scanned",
            lambda: settled(application) and window.library_text == "3 wallpapers in the library",
        )
        assert config.load().roots == (*first, extra)
        assert said == ["Scanning more walls"]
        page.add_folder(str(extra))
        assert said[-1] == "more walls is already in the library"
        assert config.load().roots == (*first, extra)
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_the_classic_shortcuts_work_in_the_new_window(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def parsed(action: str) -> set[tuple[int, int]]:
        found = set()
        for accelerator in application.get_accels_for_action(action):
            _ok, key, mods = Gtk.accelerator_parse(accelerator)
            found.add((key, int(mods)))
        return found

    def accel(text: str) -> tuple[int, int]:
        _ok, key, mods = Gtk.accelerator_parse(text)
        return key, int(mods)

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        assert accel("<Control><Shift>R") in parsed("win.random")
        assert accel("F5") in parsed("win.refresh")
        assert accel("<Control>B") in parsed("win.browse")
        assert accel("<Control>W") in parsed("window.close")
        assert accel("<Control>P") in parsed("win.palettes")
        for name in ("random", "refresh", "browse", "palettes"):
            assert isinstance(window.lookup_action(name), Gio.SimpleAction), name

        window.activate_action("win.browse", None)
        assert window.current_page is window.pages["store"]
        window.activate_action("win.palettes", None)
        assert window.current_page is window.pages["settings"]
        dialog = window.get_visible_dialog()
        assert dialog is not None, "Settings, with the palette manager open over it"
        dialog.force_close()

        window.activate_action("win.refresh", None)
        assert application._library_scan_future is not None, "F5 rescans"
        yield "the rescan", lambda: settled(application)

        runtime.answer(two_display_status())
        application.refresh_runtime_status_async()
        yield "the runtime", lambda: window.status_text == "2 displays · playing Evening"
        window.activate_action("win.random", None)
        yield "a random wallpaper", lambda: runtime.heard("random")
        yield "every tail to settle", lambda: settled(application)
        lanes.extend(application_lanes(application))
        window.activate_action("window.close", None)  # Ctrl+W

    run(application, scenario(), lanes)


def test_a_newer_store_turns_library_edits_off_and_says_why(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = paths.app_state_dir() / pairings.STATE_FILENAME
    newer.parent.mkdir(parents=True, exist_ok=True)
    newer.write_text('{"version": 99, "pairings": []}\n')
    application = Application(ui="next")
    lanes: list[object] = []
    dusk = wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield (
            "the first scan",
            lambda: (
                settled_after_a_refused_publication(application)
                and window.library_text == "2 wallpapers in the library"
            ),
        )
        assert "library changes" in window.notice.get_title()
        page = library_page(window)
        page.inspect(window.state.wallpaper(dusk))
        inspector = inspector_of(window)
        why = "Changes to wallpapers are off while pairings.json is from a newer version"
        for control in (inspector.color_mode, inspector.theme_mode, inspector.remove_button):
            assert control is not None and not control.get_sensitive()
            assert (control.get_tooltip_text() or "").startswith(why)
        for name in ("add", "remove", "reset", "new-playlist", "bulk-add"):
            action = page._action_group.lookup_action(name)
            assert isinstance(action, Gio.SimpleAction) and not action.get_enabled(), name
        page._select.set_active(True)
        page._toggle_selected(dusk)
        assert not page._fav_all.get_sensitive() and not page._remove_all.get_sensitive()
        page._select.set_active(False)
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)
