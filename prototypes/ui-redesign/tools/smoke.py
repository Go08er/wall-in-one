"""End-to-end smoke test of the prototype (headless): drives every page and demo scene,
reports exceptions. Run with tools/smoke.sh."""

import copy
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib
from wio_demo import art, data, store_catalog, thumbs, ui
from wio_demo.models import Rule, Wallpaper
from wio_demo.pages import library as library_page
from wio_demo.shell import MainWindow
from wio_demo.state import AppState

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


def settle(until, seconds: float = 5.0) -> None:
    """Run the main loop until ``until()`` is true (or the time is up)."""
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
    settle(lambda: not thumbs.LOADER.pending(), 20)


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
    dialog = settings._open_palettes()
    dialog._duplicate(state.find_palette("Nord"))
    copy = state.find_palette("Nord copy")
    assert copy is not None and copy.editable
    state.save_palette("Nord copy", "Nord mine", copy.light, copy.dark)
    dialog._apply(state.find_palette("Nord mine"))
    assert state.applied_palette().name == "Nord mine"
    dialog.delete(state.find_palette("Nord mine"))
    assert state.applied_palette() is None and state.find_palette("Nord mine") is None
    copy, undo = state.duplicate_palette("Dracula")
    undo()
    assert state.find_palette(copy.name) is None
    undo = state.delete_palette("Ayu")
    undo()
    assert state.find_palette("Ayu") is not None
    undo = state.apply_palette("Gruvbox")
    undo()
    dialog.force_close()


def playlist_actions(state, window) -> None:
    window.navigate("playlist:mc-day")
    page = window.pages["playlist"]
    undo = state.rename_playlist("mc-day", "MC days")
    assert state.playlist("mc-day").name == "MC days"
    undo()
    assert state.playlist("mc-day").name == "MC day"
    page._interval.set_selected(4)  # every hour
    assert state.playlist("mc-day").interval == 60
    page._interval.set_selected(2)
    page._shuffle.set_active(True)
    assert state.playlist("mc-day").shuffle
    page._shuffle.set_active(False)
    page.insert_entries(1, ["alpine"])
    assert state.playlist("mc-day").entries[1] == "alpine"
    page.remove_entries([1])
    page.move_entry(0, 2)
    page._list.flush()
    assert state.playlist("mc-day").entries[2] == "blocky-sunrise"
    page.move_entry(2, 0)
    page._list.flush()
    page.demo("select")
    page._move_selected_to_top()
    assert state.playlist("mc-day").entries[:2] == ["overworld-noon", "summit-glow"]
    state.reorder_entries("mc-day", ["blocky-sunrise", "overworld-noon", "alpine", "summit-glow"])
    page.play_from(2)
    assert state.current["DP-1"] == "alpine" and state.manual["DP-1"] == "mc-day"
    state.resume_schedule("all")
    # A second "Frog day" gets its own id instead of replacing the first.
    twin = state.create_playlist("Frog day")
    assert twin != "frog-day" and state.playlist("frog-day").name == "Frog day"
    window.navigate(f"playlist:{twin}")
    page.duplicate()
    copy = page._pid
    assert copy == f"{twin}-copy", copy
    assert window._nav_keys[window.sidebar.get_selected()] == f"playlist:{copy}", "the sidebar should follow"
    page._delete()
    assert not state.has_playlist(copy)
    restore = state.delete_playlist(twin)
    restore()
    assert state.has_playlist(twin)
    state.delete_playlist(twin)
    copy, _name, undo = state.duplicate_playlist("mc-night")
    undo()
    assert not state.has_playlist(copy)


def display_actions(state, window) -> None:
    window.navigate("displays")
    page = window.pages["displays"]
    if state.display_mode != "independent":
        state.set_display_mode("independent")  # the "mode:mirrored" scene linked them
    page.demo("select:HDMI-A-1")  # starts from the page's first state
    undo = state.set_display_playlist("HDMI-A-1", "mc-day")
    assert state.assigned["HDMI-A-1"] == "mc-day"
    assert state.set_display_playlist("HDMI-A-1", "mc-day") is None  # no change, no Undo
    undo()
    assert state.assigned["HDMI-A-1"] == ""
    page._assign("HDMI-A-1", "cozy-rain")
    # Linking keeps a display's playlist aside; unlinking brings it back.
    undo = state.set_display_mode("mirrored")
    assert state.current["HDMI-A-1"] == state.current["DP-1"] and state.kept_assignments() == {"HDMI-A-1": "cozy-rain"}
    state.set_display_mode("independent")
    assert state.assigned["HDMI-A-1"] == "cozy-rain" and not state.kept_assignments()
    state.set_display_mode("mirrored")
    undo()  # back to before the first switch
    assert state.display_mode == "independent" and state.assigned["HDMI-A-1"] == "cozy-rain"
    # Pausing one display, then the other, is plain "paused"; resuming one keeps the other held.
    page._toggle_pause("HDMI-A-1")
    assert state.display_paused("HDMI-A-1") and state.playback == "playing"
    page._toggle_pause("DP-1")
    assert state.playback == "paused"
    page._toggle_pause("DP-1")
    assert state.playback == "playing" and state.display_paused("HDMI-A-1") and not state.display_paused("DP-1")
    state.toggle_play()  # the player bar's Pause clears the per-display holds
    state.toggle_play()
    assert not state.display_paused("HDMI-A-1")
    page._set_color_display("HDMI-A-1")
    assert state.color_display == "HDMI-A-1"
    state.set_display_setting("DP-1", "sound", True)
    assert not state.display_settings_are_default("DP-1")
    undo = state.reset_display_settings("DP-1")
    assert state.display_settings_are_default("DP-1")
    undo()
    assert state.display_settings("DP-1")["sound"]
    page._reset_advanced("DP-1")
    undo = state.forget_display("eDP-1")
    assert [d.connector for d in state.remembered_displays] == ["DP-2"]
    undo()
    page.demo("unscheduled:HDMI-A-1")
    state.demo_set_pick("HDMI-A-1", "mc-night", 1)
    page._resume_display("HDMI-A-1")
    assert "HDMI-A-1" not in state.manual and state.current["HDMI-A-1"] == state.playlist("cozy-rain").entries[0]
    page.demo("select:DP-1")
    assert state.assigned == {"DP-1": "", "HDMI-A-1": ""} and state.color_display == "DP-1"


def schedule_actions(state, window) -> None:
    window.navigate("schedule")
    page = window.pages["schedule"]
    page.demo("")  # the demo rules
    page._save(Rule("draft", "mc-night", [2], "10:00", "11:00"), None)
    rule = state.rules[-1]
    assert rule.playlist == "mc-night" and rule.id.startswith("rule-"), rule
    edited = copy.deepcopy(rule)
    edited.playlist = "cozy-rain"
    page._save(edited, rule)
    assert state.rule(rule.id).playlist == "cozy-rain"
    assert state.update_rule(rule.id, edited) is None  # nothing changed
    other = copy.deepcopy(edited)
    other.days = [5, 6]
    undo = state.update_rule(rule.id, other)
    assert state.rule(rule.id).days == [5, 6]
    undo()
    assert state.rule(rule.id).days == [2]
    page.duplicate_rule(rule)
    page._editor.force_close()
    twin = state.rules[state.rules.index(rule) + 1]
    assert twin.playlist == rule.playlist and twin.id != rule.id
    page.move_rule(twin, -1)  # rolls in the list, then commits
    page._list.flush()
    assert state.rules.index(twin) < state.rules.index(rule)
    order = [r.id for r in state.rules]
    undo = state.reorder_rules(list(reversed(order)))
    assert [r.id for r in state.rules] == list(reversed(order))
    undo()
    assert [r.id for r in state.rules] == order
    assert state.reorder_rules(order) is None
    page._toggle(rule, False)
    settle(lambda: not state.rule(rule.id).enabled)
    assert not state.rule(rule.id).enabled
    state.set_rule_enabled(rule.id, True)
    page._fallback.set_selected(page._fallback_ids.index("mc-day"))
    assert state.fallback == "mc-day"
    undo = state.set_fallback("frog-night")
    undo()
    assert state.fallback == "mc-day" and state.set_fallback("mc-day") is None
    page.delete_rule(twin)
    undo = state.delete_rule(rule.id)
    undo()
    page.delete_rule(rule)
    assert state.rule(rule.id) is None and state.delete_rule(rule.id) is None
    page.demo("")
    assert state.fallback == "all-media" and state.rules[0].id == "daytime"


def settings_actions(state, window) -> None:
    window.navigate("settings")
    page = window.pages["settings"]
    page.demo("")
    # Folders: add (scans for a moment), locate, choose the download folder, remove.
    page._add_folder()
    added = state.folders[-1]
    assert added.path == "~/Downloads/Wallpapers" and added.scanning
    settle(lambda: not added.scanning, 3)
    assert not added.scanning
    undo = state.add_library_folder("~/Pictures/Backgrounds")
    undo()
    assert [f.path for f in state.folders][-1] == "~/Downloads/Wallpapers"
    missing = next(f for f in state.folders if f.missing)
    undo = state.locate_folder(missing.path, "/run/media/goober/Archive/wallpapers")
    assert not missing.missing
    undo()
    assert missing.missing
    first, second = state.folders[0].path, state.folders[1].path
    undo = state.set_download_folder(second)
    assert state.folders[0].path == second and state.download_folder == second
    undo()
    assert state.folders[0].path == first and state.download_folder == first
    undo = state.remove_library_folder(first)  # downloads move to the next folder
    assert state.download_folder == second
    undo()
    assert state.download_folder == first and state.folders[0].path == first
    state.remove_library_folder("~/Downloads/Wallpapers")
    # Preferences, playback defaults, colors and the template.
    state.set_setting("volume", 40)
    assert state.setting("volume") == 40
    state.set_setting("volume", 100)
    page._interval.set_selected(4)
    assert state.default_interval == 60
    page._interval.set_selected(3)
    page._battery.set_active(False)
    assert not state.stop_on_battery
    page._battery.set_active(True)
    dialog = page._open_scheme_dialog()
    dialog.pick("vibrant")
    assert state.default_scheme == "vibrant"
    dialog.pick("m3-tonal-spot")
    dialog.force_close()
    undo = state.set_default_scheme("soft")
    undo()
    assert state.default_scheme == "m3-tonal-spot"
    page._reinstall_template()
    assert state.template_status == "busy" and state.template_ok
    settle(lambda: state.template_status == "working", 3)
    state.set_template_status("missing")
    assert not state.template_ok
    state.set_template_status("working")
    page._forget_key(toast=False)
    assert not state.wallhaven_key_saved
    page._set_key_saved(True)
    page._follow.set_active(False)
    assert not state.follow_noctalia_colors
    page._follow.set_active(True)
    page._style_toggle.set_active_name("frosted")
    assert state.window_style == "frosted"
    page._style_toggle.set_active_name("solid")
    page.demo("")


def store_actions(state, window) -> None:
    window.navigate("store")
    page = window.pages["store"]
    page.demo("reset")
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
    count = len(state.wallpapers)
    page.apply_item(owned)  # already in the library: it gets a Library entry, then Apply
    wid = f"store-{owned.id}"
    assert state.has_wallpaper(wid) and len(state.wallpapers) == count + 1, (wid, count, len(state.wallpapers))
    assert state.current[state.targets()[0]] == wid, state.current
    assert wid in state.playlist("all-media").entries
    assert state.import_store_item(owned.id) == wid and len(state.wallpapers) == count + 1
    page.show_in_library(owned)
    state.resume_schedule("all")


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
            "added",
            "removed",
            "drag",
            "select",
        ],
        "schedule": [
            "edit:weekend-days",
            "new",
            "why",
            "drag",
            "december",
            "rules",
            "pick",
            "reorder",
            "enable",
            "disable:daytime",
            "assign:HDMI-A-1=cozy-rain",
            "move:evening",
            "delete:december",
            "",
        ],
        "displays": [
            "select:HDMI-A-1",
            "advanced",
            "pick",
            "assigned",
            "unscheduled",
            "identify",
            "forget",
            "mode:mirrored",
            "colors:eDP-1",
            "paused",
        ],
        "settings": [
            "search:battery",
            "group:log",
            "advanced",
            "remove-folder",
            "palettes",
            "palette-edit",
            "scheme",
            "download-folder",
            "no-key",
            "template-missing",
        ],
    }.items():
        for scene in scenes:

            def run(p=page, sc=scene):
                dialog = w().get_visible_dialog()
                if dialog is not None:
                    dialog.force_close()
                w().navigate(p)
                w().pages[p.partition(":")[0]].demo(sc)

            steps.append((f"{page}+{scene}", run))
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

    steps.append(("palette apply", lambda: (settings_page(), s().apply_palette("Nord"))))
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
    steps.append(("template missing", lambda: settings_page().demo("template-missing")))
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
