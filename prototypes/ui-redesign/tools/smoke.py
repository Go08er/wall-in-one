"""End-to-end smoke test of the prototype (headless): drives every page and demo scene,
reports exceptions. Run with tools/smoke.sh."""

import sys
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# Drive the widgets the app ships, from this checkout's src.
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk
from wio_demo import art, data, store_catalog, thumbs, ui
from wio_demo.models import Wallpaper
from wio_demo.shell import MainWindow
from wio_demo.state import AppState, DemoStoreItem

from wall_in_one.ui.next import library as library_page
from wall_in_one.ui.next.schedule_state import RuleView

failures: list[str] = []
steps_run = 0
original_hook = sys.excepthook


def hook(kind, value, tb):  # exceptions raised inside GTK callbacks
    failures.append("callback: " + "".join(traceback.format_exception(kind, value, tb))[-600:])


sys.excepthook = hook


def step(name, fn):
    global steps_run
    steps_run += 1
    try:
        fn()
    except Exception:
        failures.append(f"{name}: " + traceback.format_exc()[-800:])


# How long a wait may take. What the steps wait for is short (a demo download
# lands 1.2 s after it starts, a folder scan after 1.6 s), but it happens on
# the main loop, and a loaded machine starves that: two downloads, 2.4 s of
# timers, took 4.5 s at load 20 and 9 s at load 67, against a bound of 6 s.
# A wait ends as soon as its condition holds, so this costs a passing run
# nothing; it only bounds how long a step that will never finish takes to fail.
PATIENCE = 60.0


def settle(until, seconds: float = PATIENCE) -> None:
    """Run the main loop until ``until()`` is true (or the time is up).

    It returns either way: what follows asserts what it needs.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)


def check_thumbnails_off_main_thread(library) -> None:
    """Rebuild the Library from a cold cache: card pictures must be drawn by
    the loader's workers, and every card must have its picture afterwards."""
    threads: list[str] = []
    render = art.render

    def recording(*args):
        if args[3:] == (480, 270):
            threads.append(threading.current_thread().name)
        return render(*args)

    art.render = recording
    try:
        art.texture.cache_clear()
        thumbs.LOADER._ready.clear()
        library._rebuild()
        settle(lambda: not thumbs.LOADER.pending())
    finally:
        art.render = render
    assert thumbs.LOADER.pending() == 0, "thumbnails still pending"
    assert threads, "no card thumbnails were drawn"
    on_main = [name for name in threads if not name.startswith("thumbnails-")]
    assert not on_main, f"card thumbnails drawn on {set(on_main)}"
    blank = [card.wallpaper.id for card in library._cards.values() if card.frame.get_child()._paintable is None]
    assert not blank, f"cards still showing placeholders: {blank}"


def check_library_paging() -> None:
    """A library of 2,000 builds one page of cards; "Show more" adds a page; a new
    search or order starts from one page again; wallpapers on screen and the one
    being inspected keep their cards wherever they sort."""
    base = list(data.WALLPAPERS)
    extra = [
        Wallpaper(
            f"generated-{n:04d}",
            f"Generated {n:04d}",
            "still",
            art.STYLES[n % len(art.STYLES)],
            1000 + n,
            n % 2 == 1,
            "Local",
            "~/Pictures/Wallpapers",
            "3840 × 2160",
            "6.2 MB",
            "12 Sep",
        )
        for n in range(2000 - len(base))
    ]
    state = AppState(library=base + extra)
    page = library_page.create(state)
    size = library_page.PAGE_SIZE
    assert len(page._cards) == size, len(page._cards)
    assert thumbs.LOADER.pending() <= size, thumbs.LOADER.pending()
    assert page._more.get_visible() and page._more.get_label() == f"Show {size} more · {size} of 2000 shown"
    page._show_more()
    assert len(page._cards) == 2 * size and f"{2 * size} of 2000" in page._more.get_label()
    search = page.search

    def find(text: str) -> None:
        search.set_text(text)
        page._on_search(search)  # search-changed itself comes after a short delay

    find("generated 15")  # words match anywhere: 0015, 0150…0159, 1500…1599, …
    count = sum(1 for wallpaper in extra if "15" in wallpaper.name)
    assert page._matching == count and len(page._cards) == min(size, count), (page._matching, count)
    assert page._more.get_visible() == (count > size)
    find("generated 1500")
    assert page._matching == 1 and list(page._cards) == ["generated-1500"] and not page._more.get_visible()
    find("")
    assert len(page._cards) == size
    page._set_sort("name")  # "Lily pond" and "Rainy window" (on screen) sort past the first page
    assert len(page._cards) == size + 2 and {"lily-pond", "rain-window"} <= set(page._cards)
    page.inspect(state.wallpaper("generated-1500"))
    assert "generated-1500" in page._cards and len(page._cards) == size + 3
    page._select.set_active(True)
    page.select_all()
    assert len(page._selected) == len(page._cards) == size + 3  # "every wallpaper shown"
    page._select.set_active(False)
    settle(lambda: not thumbs.LOADER.pending())


def inspector_colors(state, library) -> None:
    library.inspect(state.wallpaper("alpine"))
    state.set_wallpaper_colors("alpine", scheme="vibrant")
    assert state.wallpaper("alpine").scheme == "vibrant"
    state.set_wallpaper_colors("alpine", mode="palette", palette="Nord")
    state.set_wallpaper_colors("alpine", theme_mode="dark")
    wallpaper = state.wallpaper("alpine")
    assert (wallpaper.color_mode, wallpaper.palette, wallpaper.theme_mode) == ("palette", "Nord", "dark")
    state.set_wallpaper_colors("alpine", mode="adaptive", scheme=None, theme_mode="auto")
    assert state.wallpaper("alpine").scheme is None


def inspector_retry(state, library) -> None:
    library.inspect(state.wallpaper("neon-rain"))
    library.inspector._retry(state.wallpaper("neon-rain"))
    assert state.wallpaper("neon-rain").problem == ""


def library_bulk_favorite(state, library) -> None:
    library.demo("select")
    library._bulk_favorite()
    assert all(state.wallpaper(wid).favorite for wid in ("lily-pond", "golden-coast", "misty-pines"))


def palette_actions(state, settings) -> None:
    """The shared palette manager over the demo: duplicate, edit, apply, schemes."""
    dialog = settings.open_palettes()
    nord = next(item for item in dialog.manager.palette_list().items if item.name == "Nord")
    dialog.edit(nord, copy=True)
    editor = dialog.nav.get_visible_page()
    assert editor.draft.renamable and editor.name.get_text() == "Nord copy"
    for taken in ("nord", "Dracula"):  # no competing names: Save waits, with the reason
        editor.name.set_text(taken)
        assert not editor.save_button.get_sensitive() and editor.problem.get_visible()
        editor.save()
    assert state.find_palette_named("nord").origin == "builtin"
    editor.name.set_text("Nord mine")
    assert editor.save_button.get_sensitive()
    editor.set_color("dark", "primary", "#123456")
    editor.save()
    mine = state.find_palette("Nord mine")
    assert mine is not None and mine.editable and mine.dark["primary"] == "#123456"
    dialog.manager.apply_catalog_palette("Nord mine")
    assert state.applied_palette().name == "Nord mine"
    dialog.edit(next(i for i in dialog.manager.palette_list().items if i.name == "Nord mine"), copy=False)
    editor = dialog.nav.get_visible_page()
    assert not editor.draft.renamable and not editor.name.get_editable()
    editor.set_color("light", "surface", "#fafafa")
    editor.save()
    assert state.find_palette("Nord mine").light["surface"] == "#fafafa"
    dialog.views.set_visible_child_name("schemes")
    assert dialog._source and all(card.preview is not None for card in dialog._cards.values())
    dialog.use_scheme("soft")
    assert state.default_scheme == "soft"
    dialog.save_scheme("vibrant")
    editor = dialog.nav.get_visible_page()
    editor.name.set_text("Mine vibrant")
    editor.save()
    assert state.find_palette("Mine vibrant") is not None
    dialog.use_scheme("m3-tonal-spot")
    dialog.search.set_text("nord")
    dialog.force_close()


def playlist_actions(state, window) -> None:
    """The app's Playlists page over the demo state (its PlaylistEditing)."""
    window.navigate("playlist:mc-day")
    page = window.pages["playlist"]
    assert page.pid == "mc-day" and [row.entry.wallpaper for row in page.rows] == state.playlist("mc-day").entries
    state.rename_playlist("mc-day", "MC days")
    assert state.playlist("mc-day").name == "MC days" and page.detail().name == "MC days"
    state.rename_playlist("mc-day", "MC day")
    page.interval_row.set_selected(4)  # Default, 5 min, 15 min, 30 min, every hour
    assert state.playlist("mc-day").interval == 60 and page.detail().rotation.interval == 3600
    page.interval_row.set_selected(0)
    assert page.detail().rotation.interval is None
    page.shuffle_row.set_selected(1)
    assert state.playlist("mc-day").shuffle
    page.shuffle_row.set_selected(0)
    page.add_entries(["alpine"], 1)
    assert state.playlist("mc-day").entries[1] == "alpine"
    page.remove_entries([page.rows[1].entry_id])
    assert state.playlist("mc-day").entries == ["blocky-sunrise", "overworld-noon", "alpine", "summit-glow"]
    page.move(page.rows[0].entry_id, 2)
    assert state.playlist("mc-day").entries[2] == "blocky-sunrise"
    page.move_to_end(page.rows[2].entry_id, -1)
    assert state.playlist("mc-day").entries[0] == "blocky-sunrise"
    page.demo("select")
    page.select_all()
    page._select.set_active(False)
    # A second "Frog day" gets its own id instead of replacing the first.
    twin = state.create_playlist("Frog day")
    assert twin != "frog-day" and state.playlist("frog-day").name == "Frog day"
    window.navigate(f"playlist:{twin}")
    page.duplicate()
    copy = page.pid
    assert copy == f"{twin}-copy", copy
    assert window._nav_keys[window.sidebar.get_selected()] == f"playlist:{copy}", "the sidebar should follow"
    state.delete_playlist(copy)
    assert not state.has_playlist(copy)
    settle(lambda: page.pid not in (None, copy))
    state.delete_playlist(twin)
    assert not state.has_playlist(twin)
    copy = state.duplicate_playlist("mc-night")
    state.delete_playlist(copy)
    assert not state.has_playlist(copy)
    window.navigate("playlist:mc-day")
    page.play()
    assert all(state.manual.get(c) == "mc-day" for c in state.targets("all"))
    state.resume_schedule("all")


def display_actions(state, window) -> None:
    """The app's Displays page over the demo: every change goes through `Scheduling`."""
    window.navigate("displays")
    page = window.pages["displays"]
    if state.display_mode != "independent":
        state.set_display_mode("independent")  # the "mode:mirrored" scene linked them
    page.flush()
    page.demo("select:HDMI-A-1")
    assert page.selected == "HDMI-A-1"
    # Its own playlist, through the row; the demo's write settles at once.
    page.plays_row.set_selected(page._plays_ids.index("mc-day"))
    assert state.display_assignments()["HDMI-A-1"] == "mc-day"
    # A playlist with nothing in it is refused, and the row shows what is saved.
    empty = state.create_playlist("Nothing here")
    page.refresh()
    page.plays_row.set_selected(page._plays_ids.index(empty))
    assert state.display_assignments()["HDMI-A-1"] == "mc-day"
    page.refresh()
    assert page._plays_ids[page.plays_row.get_selected()] == "mc-day"
    state.delete_playlist(empty)
    # Its playlist beats the shared rules, then not.
    page.refresh()
    page.beats_row.set_active(True)
    assert state.beats_global_rules("HDMI-A-1")
    page.beats_row.set_active(False)
    assert not state.beats_global_rules("HDMI-A-1")
    # Desktop colors, and the mode both ways.
    page.use_for_colors("HDMI-A-1")
    assert state.color_display == "HDMI-A-1"
    page.use_for_colors("DP-1")
    page.mode_group.set_active_name("mirrored")
    assert state.display_mode == "mirrored"
    page.refresh()
    assert page.linked_note.get_visible() and not page.plays_row.get_visible()
    page.mode_group.set_active_name("independent")
    assert state.display_mode == "independent"
    # Runtime choices: play a playlist now, then resume.
    page.refresh()
    page.play_row.set_selected(page._play_ids.index("mc-night"))
    assert state.manual.get("HDMI-A-1") == "mc-night"
    page._resume("HDMI-A-1")
    assert "HDMI-A-1" not in state.manual
    # A display that is not connected is kept until it is forgotten.
    assert "eDP-1" in page.remembered_rows
    page.forget("eDP-1")
    assert [d.connector for d in state.remembered_displays] == ["DP-2"]
    page.flush()
    assert "eDP-1" not in page.remembered_rows
    state.assign_display("HDMI-A-1", "")
    page.demo("select:DP-1")
    assert state.display_assignments().get("HDMI-A-1") is None and state.color_display == "DP-1"


def schedule_actions(state, window) -> None:
    """The app's Schedule page over the demo: the same writes the real adapter takes."""
    window.navigate("schedule")
    page = window.pages["schedule"]
    page.flush()
    count, fallback = len(state.rules), state.fallback
    saved: list[bool] = []
    page.save(RuleView("", "mc-night", weekdays=frozenset({2}), start=600, end=660), None, saved.append)
    assert saved == [True], saved
    rule = state.rules[-1]
    assert (rule.playlist, rule.days, rule.start, rule.end) == ("mc-night", [2], "10:00", "11:00"), rule
    view = state.schedule_rule(rule.id)
    assert view is not None and view.weekdays == {2} and view.start == 600 and not view.months
    # December is month 12 here and 11 in the demo's own data.
    page.save(replace(view, playlist="cozy-rain", months=frozenset({12})), view, saved.append)
    assert state.rule(rule.id).playlist == "cozy-rain" and state.rule(rule.id).months == [11]
    # A window that starts and ends on the same minute is refused, never stored as "always".
    page.save(replace(view, start=600, end=600), view, saved.append)
    assert saved[-1] is False and state.rule(rule.id).start == "10:00"
    # A drag over no time makes no rule; a whole-day drag is "all day".
    page.create_from_calendar([1], 600, 600)
    assert page.editor is None
    page.create_from_calendar([1], 0, 24 * 60)
    assert page.editor is not None and page.editor.draft.start is None
    page.editor.force_close()
    # Duplicate lands one place up; moving it down puts it below the original.
    page.duplicate_rule(rule.id)
    ids = [r.id for r in state.rules]
    twin = ids[ids.index(rule.id) + 1]
    page.move_rule(twin, -1)
    ids = [r.id for r in state.rules]
    assert ids.index(twin) < ids.index(rule.id)
    # Dragging the bottom rule to the top of the list makes it the last stored rule.
    page.flush()
    settle(lambda: page.rule_list.row_heights() and min(page.rule_list.row_heights()) > 0)
    rows = page.rule_list.get_rows()
    heights = page.rule_list.row_heights()
    bottom = rows[-1].rule.id
    page.rule_list.begin_drag(rows[-1])
    page.rule_list.update_drag(-(sum(heights[:-1]) + heights[-1] / 2 - heights[0] * 0.4))
    page.rule_list.end_drag()
    settle(lambda: not page.rule_list.busy)
    assert state.rules[-1].id == bottom, [r.id for r in state.rules]
    page.move_rule(bottom, -(len(state.rules) - 1))
    # The switch on the row.
    page.flush()
    page.row(rule.id).switch.set_active(False)
    assert not state.rule(rule.id).enabled
    page.flush()
    assert not page.row(rule.id).switch.get_active()
    # When nothing is scheduled.
    page.fallback_row.set_selected(page._fallback_ids.index("mc-day"))
    assert state.fallback == "mc-day"
    page.delete_rule(twin)
    page.delete_rule(rule.id)
    assert len(state.rules) == count
    state.set_fallback(fallback)
    # "Why?" names the rule the (simulated) runtime decides by.
    page.flush()
    page.build_why()
    assert page.why_entries


def settings_actions(state, window) -> None:
    """The shared Settings page over the demo: rows save by real keys, folders, the key."""
    window.navigate("settings")
    page = window.pages["settings"]
    page.demo("")
    # Folders: add, make one the download folder, remove (confirmed).
    page.add_folder("~/Downloads/Wallpapers")
    assert state.folders[-1].path == "~/Downloads/Wallpapers"
    settle(lambda: not state.folders[-1].scanning)
    page.add_folder("~/Downloads/Wallpapers")  # already there: nothing added
    assert [f.path for f in state.folders].count("~/Downloads/Wallpapers") == 1
    first, second = state.folders[0].path, state.folders[1].path
    page.make_download_folder(second)
    assert state.folders[0].path == second and state.download_folder == second
    page.make_download_folder(first)
    dialog = page.confirm_remove("~/Downloads/Wallpapers")
    dialog.emit("response", "remove")
    assert "~/Downloads/Wallpapers" not in [f.path for f in state.folders]
    # Rows save their real settings keys, and show what is saved.
    rows = {binding.key: binding.row for binding in page._bindings}
    rows["video_volume"].set_value(40)
    assert state.setting("video_volume") == 40
    rows["video_volume"].set_value(100)
    rows["cycle_interval"].set_value(3600)
    assert state.default_interval == 60
    rows["cycle_interval"].set_value(1800)
    rows["stop_animations_on_battery"].set_active(False)
    assert not state.stop_on_battery
    rows["stop_animations_on_battery"].set_active(True)
    rows["follow_noctalia_palette"].set_active(False)
    assert not state.follow_noctalia_colors
    rows["follow_noctalia_palette"].set_active(True)
    rows["preview_scheme"].set_selected(5)  # vibrant
    assert state.default_scheme == "vibrant"
    rows["preview_scheme"].set_selected(0)
    rows["scene_scaling"].set_selected(3)  # fill
    assert state.setting("scene_scaling") == "fill"
    # The Wallhaven key.
    page.clear_key()
    assert not state.wallhaven_key_saved
    page._key_entry.set_text("short")
    page.save_key()
    assert not state.wallhaven_key_saved and page._key_entry.has_css_class("error")
    page._key_entry.set_text("a" * 32)
    page.save_key()
    assert state.wallhaven_key_saved and page._key_entry.get_text() == ""
    # Noctalia's template: the row shows its state; only the button installs.
    state.set_template_status("missing")
    state.check_template()
    assert page._template_button.get_visible() and page._template_button.get_label() == "Install"
    page.install_template()
    assert state.template_status().busy and not page._template_button.get_sensitive()
    settle(lambda: state.template_status().state == "installed")
    assert state.template_status().state == "installed" and not page._template_button.get_visible()
    # The window's look.
    page._style_toggle.set_active_name("frosted")
    assert state.window_style == "frosted"
    page._style_toggle.set_active_name("solid")
    page.demo("")


def store_actions(state, window) -> None:
    window.navigate("store")
    page = window.pages["store"]  # the app's shared Store page (wall_in_one.ui.next.store)
    page.demo("reset")
    settle(lambda: not page.loading)
    assert page.cards and len(page.cards) <= 40, len(page.cards)
    results = state.store_search(store_catalog.Query("Wallhaven", text="night"))
    assert results and all(item.provider == "Wallhaven" for item in results)
    source = state.store_item("wallhaven-3")
    like = state.store_like_source(f"like:{store_catalog.site_id(source)}")
    assert like is source
    owned = next(
        item
        for item in state.store_search(store_catalog.Query("Wallhaven"))
        if item.in_library and not state.has_wallpaper(f"store-{item.id}")
    )
    view = DemoStoreItem(owned)
    assert page.store.in_library(view)
    count = len(state.wallpapers)
    page.apply_item(view)  # already in the library: it gets a Library entry, then Apply
    wid = f"store-{owned.id}"
    assert state.has_wallpaper(wid) and len(state.wallpapers) == count + 1, (wid, count, len(state.wallpapers))
    assert state.current[state.targets()[0]] == wid, state.current
    assert wid in state.playlist("all-media").entries
    assert state.import_store_item(owned.id) == wid and len(state.wallpapers) == count + 1
    page.show_in_library(view)
    state.resume_schedule("all")


def store_paging(window) -> None:
    """Bounded pages with Previous/Next: the next provider page is asked for on Next."""
    window.navigate("store")
    page = window.pages["store"]
    page.demo("reset")
    settle(lambda: not page.loading)
    first = [item.id for item in page.pages.current]
    assert first and page.pages.can_next and not page.pages.can_previous
    page.show_next()
    settle(lambda: not page.fetching and page.pages.index == 1)
    assert page.pages.index == 1 and page.pages.can_previous, page.pages.label()
    assert 0 < len(page.cards) <= 40 and not set(page.cards) & set(first)
    page.show_previous()
    assert [item.id for item in page.pages.current] == first


def store_download(window) -> None:
    """One download at a time, then "In library" on its card."""
    window.navigate("store")
    page = window.pages["store"]
    page.demo("reset")
    settle(lambda: not page.loading)
    targets = [card.item for card in page.cards.values() if card.can_pick][:2]
    for item in targets:
        page.download(item)
    assert page.store.downloads.busy == tuple(item.id for item in targets)
    assert page.cards[targets[0].id].busy == "downloading"
    assert page.cards[targets[1].id].busy == "waiting"
    assert page.queue_label.get_label() == "Downloading 1 of 2"
    settle(lambda: not page.store.downloads.busy)
    assert not page.store.downloads.busy, f"the downloads never finished ({PATIENCE:g} s)"
    assert all(page.cards[item.id].in_library for item in targets)


def window_actions(state, window) -> None:
    window.navigate("library")
    window.activate_action("win.dark", None)  # the ☰ menu's dark style switch
    assert not state.dark
    window.activate_action("win.dark", None)
    assert state.dark
    window.activate_action("win.style", GLib.Variant("s", "translucent"))
    assert state.window_style == "translucent"
    window.activate_action("win.style", GLib.Variant("s", "solid"))
    state.set_use_live_colors(False)  # no real Noctalia in the smoke test
    assert not state.live_colors()
    state.set_scope("HDMI-A-1")
    assert state.targets() == ["HDMI-A-1"]
    state.set_scope("all")


def finish_page_change(window) -> None:
    """Let the shell's page crossfade end before the new page's scene runs.

    The old page stays mapped, with any menu it has open, for the 120 ms of the
    crossfade, and steps are only 60 ms apart. Wayland allows a new menu only
    on top of the last one, so a scene that opened its menu inside that window
    made GTK refuse it ("Tried to map a grabbing popup with a non-top most
    parent"). A person's next click comes after the crossfade; this waits for
    it, boundedly. A menu left open after the page has gone would still show
    here: it stays open and the next scene's menu then warns.
    """
    settle(lambda: not window.stack.get_transition_running(), 5.0)


def check_screenshot_draws_popovers(window) -> None:
    """``demo.py --screenshot`` must show a menu that is open: it has a surface of
    its own, so the window's own image never holds it."""
    import demo  # the screenshot code itself

    window.navigate("displays")
    finish_page_change(window)
    window.pages["displays"].demo("menu:HDMI-A-1")
    settle(lambda: demo.mapped_popovers(window) and window.get_width() > 0)
    assert demo.mapped_popovers(window), "the menu never mapped"
    settle(lambda: False, 0.3)  # a frame to lay out and place it

    def png(snapshot) -> bytes:
        node, area = snapshot.to_node(), Graphene.Rect().init(0, 0, window.get_width(), window.get_height())
        assert node is not None
        return window.get_renderer().render_texture(node, area).save_to_png_bytes().get_data()

    plain = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(plain, window.get_width(), window.get_height())
    assert png(plain) != png(demo.snapshot_window(window)), "the menu is not in the screenshot"


def build_steps(app, holder):
    w = lambda: holder["window"]  # noqa: E731
    s = lambda: holder["window"].state  # noqa: E731
    steps = []
    for key in ("library", "store", "playlist:frog-day", "playlist:all-media", "schedule", "displays", "settings"):
        steps.append((f"navigate {key}", lambda k=key: w().navigate(k)))
    lib = lambda: w().pages["library"]  # noqa: E731
    steps.append(
        ("thumbnails off the main thread", lambda: (w().navigate("library"), check_thumbnails_off_main_thread(lib())))
    )
    steps.append(("library paging", check_library_paging))
    for wallpaper in data.WALLPAPERS:
        steps.append((f"inspect {wallpaper.id}", lambda wp=wallpaper: (w().navigate("library"), lib().inspect(wp))))
    for mode in ("palette", "keep", "adaptive"):

        def color(m=mode):
            s().set_wallpaper_colors("alpine", mode=m)
            lib().inspector.show(s().wallpaper("alpine"))

        steps.append((f"color mode {mode}", color))
    steps.append(("inspector colors", lambda: inspector_colors(s(), lib())))
    steps.append(("inspector retry", lambda: inspector_retry(s(), lib())))
    steps.append(("library bulk favorite", lambda: library_bulk_favorite(s(), lib())))
    for key in ("still", "video", "scene", "all"):
        steps.append((f"filter {key}", lambda k=key: lib()._kinds.set_active_name(k)))
    for key in ("name", "kind", "color", "added"):
        steps.append((f"sort {key}", lambda k=key: lib()._set_sort(k)))
    steps += [
        ("size small", lambda: lib()._set_size("small")),
        ("size large", lambda: lib()._set_size("large")),
        ("search", lambda: lib().search.set_text("frog")),
        ("search clear", lambda: lib().search.set_text("")),
        ("select + bulk add", lambda: (lib().demo("select"), lib()._bulk_add("cozy-rain"))),
        ("apply all", lambda: s().apply("alpine", "all")),
        ("apply one display", lambda: s().apply("dune-sea", "HDMI-A-1")),
        ("favorite", lambda: s().toggle_favorite("dune-sea")),
        ("resume schedule", lambda: s().resume_schedule("all")),
        ("play playlist", lambda: s().play_playlist("mc-day")),
        ("next/prev/random", lambda: (s().step(1), s().step(-1), s().random())),
        ("pause/stop/play", lambda: (s().toggle_play(), s().stop(), s().toggle_play())),
        ("shuffle/rotate", lambda: (s().set_shuffle(True), s().set_rotate(False), s().set_rotate(True))),
        (
            "empty playlist play",
            lambda: (
                w().navigate(f"playlist:{s().create_playlist('Empty X')}"),
                s().play_playlist("empty-x"),
            ),
        ),
        ("battery on/off", lambda: (s().set_battery(True), s().set_battery(False))),
        ("service off/on", lambda: (s().set_service_running(False), s().set_service_running(True))),
        ("mirrored", lambda: s().set_display_mode("mirrored")),
        ("independent", lambda: s().set_display_mode("independent")),
        ("light", lambda: s().set_dark(False)),
        ("dark", lambda: s().set_dark(True)),
    ]
    for page, scenes in {
        "store": [
            "search:night",
            "provider:MotionBGS",
            "downloading",
            "preview:wallhaven-1",
            "error",
            "rate-limit",
            "filters",
            "select",
            "saved",
            "like:wallhaven-3",
            "nsfw-key",
            "reset",
        ],
        "playlist:frog-day": [
            "picker",
            "menu:2",
            "select",
            "compact",
            "rename",
            "delete",
            "new",
            "picker-search:pond",
            "added",
            "removed",
            "drag",
            "select",
            "playlist-menu",
        ],
        "schedule": [
            "edit:weekend-days",
            "editend:weekend-days",
            "new",
            "why",
            "drag",
            "week:1",
            "seasonal",
            "menu:daytime",
            "reorder",
            "display:HDMI-A-1",
            "",
        ],
        "displays": [
            "select:HDMI-A-1",
            "menu:HDMI-A-1",
            "forget",
            "select:DP-1",
        ],
        "settings": [
            "search:battery",
            "search:nothing-matches-this",
            "group:colors",
            "advanced",
            "remove-folder",
            "palettes",
            "schemes",
            "no-key",
        ],
    }.items():
        for scene in scenes:

            def run(p=page, sc=scene):
                dialog = w().get_visible_dialog()
                if dialog is not None:
                    dialog.force_close()
                w().navigate(p)
                finish_page_change(w())
                w().pages[p.partition(":")[0]].demo(sc)

            steps.append((f"{page}+{scene}", run))
        if page == "store":
            # The demo's downloads land on timers, as real ones do: let them
            # land here, not in the middle of the next page's scenes.
            steps.append(("store downloads land", lambda: settle(lambda: not s().downloads.busy)))
    steps.append(("the screenshot draws an open menu", lambda: check_screenshot_draws_popovers(w())))
    steps.append(
        (
            "select pill library",
            lambda: (
                w().navigate("library"),
                w().pages["library"]._select.set_active(True),
                w().pages["library"]._select.set_active(False),
            ),
        )
    )
    steps.append(
        (
            "select pill store",
            lambda: (
                w().navigate("store"),
                w().pages["store"]._select.set_active(True),
                w().pages["store"]._select.set_active(False),
            ),
        )
    )
    steps.append(
        (
            "library select all",
            lambda: (
                w().navigate("library"),
                w().pages["library"]._select.set_active(True),
                w().pages["library"].select_all(),
            ),
        )
    )
    steps.append(
        (
            "library remove two",
            lambda: w().pages["library"]._remove([s().wallpaper("dune-sea"), s().wallpaper("alpine")]),
        )
    )
    steps.append(("library select off", lambda: w().pages["library"]._select.set_active(False)))
    steps.append(("playlist actions", lambda: playlist_actions(s(), w())))
    steps.append(("display actions", lambda: display_actions(s(), w())))
    steps.append(("schedule actions", lambda: schedule_actions(s(), w())))
    steps.append(("settings actions", lambda: settings_actions(s(), w())))
    steps.append(("store actions", lambda: store_actions(s(), w())))
    steps.append(("store paging", lambda: store_paging(w())))
    steps.append(("store download", lambda: store_download(w())))
    steps.append(("window actions", lambda: window_actions(s(), w())))
    steps.append(
        (
            "store select all",
            lambda: (
                w().navigate("store"),
                w().pages["store"]._select.set_active(True),
                w().pages["store"].select_all(),
                w().pages["store"]._select.set_active(False),
            ),
        )
    )
    for style in ("frosted", "translucent", "frosted"):
        steps.append((f"style {style}", lambda st=style: s().set_window_style(st)))
    steps.append(("panel 40%", lambda: s().set_panel_opacity(0.4)))
    steps.append(("background 10%", lambda: s().set_background_opacity(0.1)))
    steps.append(
        (
            "panel below background",
            lambda: (
                s().set_background_opacity(0.8),
                s().set_panel_opacity(0.2),
                s().emit_changed("appearance"),
            ),
        )
    )
    for frost in (0.0, 0.25, 1.0):
        steps.append((f"frost {frost}", lambda f=frost: s().set_frost(f)))
    steps.append(
        (
            "all clear frosted",
            lambda: (
                s().set_panel_opacity(0.0),
                s().set_background_opacity(0.0),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(("menu glass settings", lambda: w().activate_action("win.glass-settings", None)))

    def settings_page():
        w().navigate("settings")
        return w().pages["settings"]

    steps.append(("palette apply", lambda: (settings_page(), s().palette_manager.apply_catalog_palette("Nord"))))
    steps.append(("palette dialog", lambda: settings_page().demo("palettes")))
    steps.append(("palette actions", lambda: palette_actions(s(), settings_page())))
    steps.append(
        (
            "desktop colors off",
            lambda: (settings_page(), s().set_desktop_colors(False)),
        )
    )
    steps.append(
        (
            "desktop colors on",
            lambda: (settings_page(), s().set_desktop_colors(True)),
        )
    )
    steps.append(
        (
            "keep wallpaper",
            lambda: (s().set_wallpaper_colors("dune-sea", mode="keep"), s().apply("dune-sea", "all")),
        )
    )
    steps.append(
        (
            "style solid opacity",
            lambda: (
                s().set_window_style("solid"),
                s().set_panel_opacity(0.2),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(
        (
            "style translucent floor",
            lambda: (
                s().set_window_style("translucent"),
                s().set_background_opacity(0.0),
                s().emit_changed("appearance"),
            ),
        )
    )
    steps.append(("style frosted again", lambda: s().set_window_style("frosted")))
    steps.append(("frosted light", lambda: s().set_dark(False)))
    steps.append(("frosted dark", lambda: s().set_dark(True)))
    # A simulated day with the schedule page showing, then the library.
    steps.append(("show schedule", lambda: w().navigate("schedule")))
    for tick in range(36):
        steps.append((f"tick {tick}", lambda: s().advance(40)))
    steps.append(("show library", lambda: w().navigate("library")))
    for tick in range(12):
        steps.append((f"tick L{tick}", lambda: s().advance(60)))

    def go_narrow():
        old = holder["window"]
        fresh = MainWindow(app, AppState())
        fresh.set_default_size(420, 860)
        fresh.present()
        old.destroy()
        holder["window"] = fresh

    steps.append(("narrow window", go_narrow))
    for key in ("library", "store", "playlist:frog-day", "schedule", "displays", "settings"):
        steps.append((f"narrow {key}", lambda k=key: w().navigate(k)))
    steps.append(
        ("narrow inspector", lambda: (w().navigate("library"), w().pages["library"].inspect(s().wallpaper("alpine"))))
    )
    return steps


def main():
    app = Adw.Application(application_id="dev.goober.WallInOne.Smoke")
    holder = {}

    def activate(application):
        ui.load_css()
        holder["window"] = MainWindow(application, AppState())
        holder["window"].present()
        queue = build_steps(application, holder)

        def run_next():
            if not queue:
                GLib.timeout_add(500, application.quit)
                return False
            name, fn = queue.pop(0)
            step(name, fn)
            GLib.timeout_add(60, run_next)
            return False

        GLib.timeout_add(800, run_next)

    app.connect("activate", activate)
    app.run([sys.argv[0]])
    print(f"SMOKE steps={steps_run} failures={len(failures)}")
    for failure in failures[:12]:
        print("----\n" + failure)
    Path(sys.argv[1] if len(sys.argv) > 1 else "/dev/null").write_text(str(len(failures)))


if __name__ == "__main__":
    main()
